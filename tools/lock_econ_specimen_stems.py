"""
tools/lock_econ_specimen_stems.py
==================================
PHASE: build

Follow-up to Stage 4 of the mark scheme pipeline.

tools/ingest_econ_specimen_stems.py wrote 24 -stem chunk rows for the
Economics Specimen 1 paper, but its own docstring admits the question
prompts were RECONSTRUCTED from the mark-scheme answers, not extracted from
the real question paper -- every one of those rows carries page=NULL.
tools/ingest_econ_specimen_questions.py later re-extracted the REAL,
page-tagged question text from pages 73-84 of the same syllabus PDF into a
reviewable CSV (04_REPORTS/Economics_specimen_stems_review.csv) but wrote
nothing to the database.

This script is the lock step: it loads that CSV -- once Ricky has verified
every row against the source PDF -- and replaces the fabricated page=NULL
chunk rows with the real page + text, following the same verification
discipline as tools/lock_mark_scheme.py.

For each CSV row (keyed by its "question_id" column, which is also the
target chunk_id):
  - If a chunks row already exists under this exact chunk_id (the
    fabricated page=NULL version), its page + chunk_text are UPDATEd in
    place -- never deleted/reinserted, so any FK references survive.
  - Else, if this chunk_id is a known rename target (see
    _LEGACY_CHUNK_ID_RENAMES below) and a chunks row exists under the
    LEGACY id, that row is renamed (UPDATE chunk_id + page + chunk_text) --
    also never delete/reinsert.
  - Else a brand-new chunks row is INSERTed, using the same column pattern
    as the existing -stem rows: (doc_id, objective_id, subject_id,
    chunk_text, page, question_num, chunk_id). doc_id is taken from the
    existing Economics specimen documents row (created by
    ingest_econ_specimen_stems.py); objective_id is the primary
    (alphabetically-first) objective_id already bound to this question_id
    in mark_points.

Guards (same discipline as lock_mark_scheme.py):
  - Refuses to run if any row has verified != 1.
  - Refuses to run if any row has a blank or non-integer page value
    (Rule 2 -- every row served to the student must cite a real source
    page).

Usage:
    python tools/lock_econ_specimen_stems.py
    python tools/lock_econ_specimen_stems.py --dry-run
    python tools/lock_econ_specimen_stems.py --csv-path <override path>
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(_REPO_ROOT / ".env")

from backend.app import open_db, apply_runtime_migrations
from backend.db.backup import backup_first

SUBJECT_ID = "Economics"
CSV_NAME = "Economics_specimen_stems_review.csv"

# tools/fix_econ_q6_block_realignment.py's docstring records that
# "ECON-qb5(d)v1-stem" (the real Q5(d) electronic-banking content) was
# originally inserted under the stale id "ECON-qb6(d)v1-stem" -- a leftover
# from the bogus pre-realignment block numbering -- before a follow-up pass
# renamed the STEM_TEXTS key to match its real content. If a chunks row
# still exists under the old id, the corresponding CSV row is a RENAME, not
# a fresh insert.
_LEGACY_CHUNK_ID_RENAMES = {
    "ECON-qb5(d)v1-stem": "ECON-qb6(d)v1-stem",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def _fld(r: dict, k: str) -> str:
    return (r.get(k) or "").strip()


def _describe(r: dict) -> str:
    return (
        f"{_fld(r, 'question_id')!r}  page={_fld(r, 'page')!r}  "
        f"verified={_fld(r, 'verified')!r}  | {_fld(r, 'stem_text')[:60]!r}"
    )


# ── guards ─────────────────────────────────────────────────────────────────────

def check_unverified_rows(rows: list) -> list:
    """Return rows where verified != '1'."""
    return [r for r in rows if _fld(r, "verified") != "1"]


def check_bad_page_rows(rows: list) -> list:
    """Return rows with a blank or non-integer page value."""
    return [r for r in rows if not _fld(r, "page").isdigit()]


# ── core logic (importable for tests) ─────────────────────────────────────────

def classify_rows(db: sqlite3.Connection, rows: list) -> dict:
    """Partition rows into 'update', 'rename', 'insert' groups.

    Returns {'update': [(row, chunk_id), ...],
             'rename': [(row, legacy_chunk_id), ...],
             'insert': [(row, None), ...]}.

    Checked in order per row: does a chunks row already exist under the
    row's own chunk_id (update)? If not, does one exist under a known legacy
    id for this chunk_id (rename)? If neither, it's a fresh insert.
    """
    update, rename, insert = [], [], []
    for r in rows:
        chunk_id = _fld(r, "question_id")
        existing = db.execute(
            "SELECT 1 FROM chunks WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
        if existing:
            update.append((r, chunk_id))
            continue

        legacy_id = _LEGACY_CHUNK_ID_RENAMES.get(chunk_id)
        if legacy_id:
            legacy_existing = db.execute(
                "SELECT 1 FROM chunks WHERE chunk_id = ?", (legacy_id,)
            ).fetchone()
            if legacy_existing:
                rename.append((r, legacy_id))
                continue

        insert.append((r, None))
    return {"update": update, "rename": rename, "insert": insert}


def _primary_objective_for_question(db: sqlite3.Connection, question_id: str) -> str | None:
    """Alphabetically-first objective_id bound to this question_id in mark_points."""
    row = db.execute(
        "SELECT MIN(objective_id) AS obj FROM mark_points WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    return row["obj"] if row and row["obj"] else None


def _specimen_doc_id(db: sqlite3.Connection) -> str:
    """doc_id of the existing Economics specimen documents row.

    Created by tools/ingest_econ_specimen_stems.py -- raises if that script
    has never been run, since there is nothing sensible to attach a new
    stem chunk to otherwise.
    """
    row = db.execute(
        "SELECT doc_id FROM documents WHERE subject_id = ? AND content_type = 'specimen' "
        "ORDER BY doc_id LIMIT 1",
        (SUBJECT_ID,),
    ).fetchone()
    if not row:
        raise RuntimeError(
            "No Economics specimen document row found -- run "
            "tools/ingest_econ_specimen_stems.py first to create it."
        )
    return row["doc_id"]


def apply_changes(db: sqlite3.Connection, groups: dict) -> dict:
    """Write the update/rename/insert groups to the DB in one transaction.

    Returns counts: {'updated', 'renamed', 'inserted', 'skipped_no_objective'}.
    """
    counts = {"updated": 0, "renamed": 0, "inserted": 0, "skipped_no_objective": 0}

    for r, chunk_id in groups["update"]:
        page = int(_fld(r, "page"))
        text = _fld(r, "stem_text")
        db.execute(
            "UPDATE chunks SET page = ?, chunk_text = ? WHERE chunk_id = ?",
            (page, text, chunk_id),
        )
        counts["updated"] += 1

    for r, legacy_id in groups["rename"]:
        new_chunk_id = _fld(r, "question_id")
        page = int(_fld(r, "page"))
        text = _fld(r, "stem_text")
        db.execute(
            "UPDATE chunks SET chunk_id = ?, page = ?, chunk_text = ? WHERE chunk_id = ?",
            (new_chunk_id, page, text, legacy_id),
        )
        counts["renamed"] += 1

    if groups["insert"]:
        doc_id = _specimen_doc_id(db)
        for r, _ in groups["insert"]:
            chunk_id = _fld(r, "question_id")
            num = _fld(r, "question_num")
            part = _fld(r, "question_part")
            question_num_full = f"{num}{part}" if part else num
            page = int(_fld(r, "page"))
            text = _fld(r, "stem_text")

            objective_id = _primary_objective_for_question(db, chunk_id)
            if not objective_id:
                print(f"  WARNING: no mark_points found for {chunk_id!r} -- skipping insert")
                counts["skipped_no_objective"] += 1
                continue

            db.execute(
                """
                INSERT INTO chunks
                    (doc_id, objective_id, subject_id, chunk_text, page,
                     question_num, chunk_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (doc_id, objective_id, SUBJECT_ID, text, page, question_num_full, chunk_id),
            )
            counts["inserted"] += 1

    db.commit()
    return counts


def run_live_verification(db: sqlite3.Connection, subject_id: str) -> dict:
    """Post-write live-DB check.

    1. Confirms zero {subject_id} -stem chunks have page=NULL.
    2. Runs the exact /api/questions/{subject_id} quiz-picker SQL
       (backend/app.py) and reports the row count it now returns.
    """
    null_page_count = db.execute(
        """
        SELECT COUNT(*) FROM chunks
        WHERE  subject_id = ? AND chunk_id LIKE '%-stem' AND page IS NULL
        """,
        (subject_id,),
    ).fetchone()[0]

    picker_rows = db.execute(
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

    return {
        "null_page_count": null_page_count,
        "picker_question_count": len(picker_rows),
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

@backup_first("pre_lock_econ_specimen_stems")
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Lock the reviewed Economics specimen stem CSV into chunks, "
                     "replacing the fabricated page=NULL stems."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate and preview without writing to the DB")
    ap.add_argument("--csv-path", default=None,
                    help="Override the review CSV path (default: "
                         "{REPORTS_ROOT}/Economics_specimen_stems_review.csv)")
    args = ap.parse_args()

    reports_root = os.getenv("REPORTS_ROOT")
    db_path = os.getenv("DB_PATH")
    if not args.csv_path and not reports_root:
        sys.exit("ERROR: REPORTS_ROOT not set in .env")
    if not db_path:
        sys.exit("ERROR: DB_PATH not set in .env")

    csv_path = Path(args.csv_path) if args.csv_path else Path(reports_root) / CSV_NAME
    if not csv_path.exists():
        sys.exit(
            f"ERROR: CSV not found: {csv_path}\n"
            "Run tools/ingest_econ_specimen_questions.py and review it first."
        )

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"Read {len(rows)} rows from {csv_path.name}")

    # Gate 1: every row must be verified
    unverified = check_unverified_rows(rows)
    if unverified:
        print(f"\nERROR: {len(unverified)} row(s) are not verified (verified != 1):")
        for r in unverified:
            print(f"  {_describe(r)}")
        sys.exit("Every row must be verified=1 before locking.")

    # Gate 2: Rule 2 -- every row must cite a real integer source page
    bad_pages = check_bad_page_rows(rows)
    if bad_pages:
        print(f"\nERROR: {len(bad_pages)} row(s) have a blank or non-integer page value:")
        for r in bad_pages:
            print(f"  {_describe(r)}")
        sys.exit("Populate a real integer source page for all rows before locking (Rule 2).")

    db = open_db(db_path)
    apply_runtime_migrations(db)

    try:
        groups = classify_rows(db, rows)

        print("\nPreview:")
        print(f"  UPDATE  ({len(groups['update'])} row(s)):")
        for r, chunk_id in groups["update"]:
            existing = db.execute(
                "SELECT page FROM chunks WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()
            old_page = existing["page"] if existing else None
            print(f"    {chunk_id}  page {old_page!r} -> {_fld(r, 'page')}")

        print(f"  RENAME  ({len(groups['rename'])} row(s)):")
        for r, legacy_id in groups["rename"]:
            existing = db.execute(
                "SELECT page FROM chunks WHERE chunk_id = ?", (legacy_id,)
            ).fetchone()
            old_page = existing["page"] if existing else None
            new_chunk_id = _fld(r, "question_id")
            print(f"    {legacy_id} -> {new_chunk_id}  page {old_page!r} -> {_fld(r, 'page')}")

        print(f"  INSERT  ({len(groups['insert'])} row(s)):")
        for r, _ in groups["insert"]:
            print(f"    {_fld(r, 'question_id')}  page None -> {_fld(r, 'page')}")

        if args.dry_run:
            print("\n[dry-run] Validation + preview only. No DB changes.")
            return

        answer = input("\nApply these changes? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted. No changes written.")
            return

        counts = apply_changes(db, groups)
        print(
            f"\nUpdated {counts['updated']}, renamed {counts['renamed']}, "
            f"inserted {counts['inserted']}, skipped {counts['skipped_no_objective']} "
            "(no matching mark_points)."
        )

        print("\nLive verification:")
        verification = run_live_verification(db, SUBJECT_ID)
        print(f"  Economics -stem chunks with page=NULL: {verification['null_page_count']}")
        print(f"  /api/questions/{{subject_id}} picker rows for Economics: "
              f"{verification['picker_question_count']}")
        if verification["null_page_count"] != 0:
            print("  WARNING: expected zero -- some -stem chunks still have page=NULL.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
