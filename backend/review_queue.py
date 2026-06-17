# PHASE: build
"""
backend/review_queue.py
=======================
Stage 8 (Build Playbook v3.1) -- review CLI for the ingest_review_queue.

The recovery pass (recover_mark_points.py) sends low-confidence extractions here
instead of writing them straight into mark_points. This tool walks those queued
rows one at a time so a NON-EXPERT reviewer can clear them:

    Y -> promote the candidate into mark_points (and remove it from the queue)
    N -> delete the candidate (reject it)
    Q -> quit, leaving the rest of the queue untouched

The reviewer needs no subject knowledge. They only judge whether the text LOOKS
like a CSEC mark point -- brief, examiner-phrased, a single award-worthy idea --
not whether it is academically correct. Anything that looks like a cover page,
an OCR mess, or a duplicate gets an N.

Run:
    python backend/review_queue.py --subject Principles_of_Business
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from recover_mark_points import (  # noqa: E402
    SOURCE_TYPE,
    _mark_point_id,
    ensure_recovery_columns,
)
from derive_syllabus_mark_points import (  # noqa: E402
    SOURCE_TYPE as SYLLABUS_SOURCE_TYPE,
    REVIEW_REASON as SYLLABUS_REVIEW_REASON,
)

# Two producers feed this queue and each annotates evidence differently:
#   recover_mark_points.py  -> "<point_text>\n\nEvidence: <quote>"
#   derive_syllabus_*.py    -> "<point_text> | EVIDENCE: <quote>"
# Split on whichever marker is present so the promoted mark_point holds the clean
# point text, not the evidence annotation.
EVIDENCE_MARKERS = ("\n\nEvidence:", " | EVIDENCE: ")

# A queued row's reason decides what source_type its promoted mark_point carries.
# Anything not explicitly mapped (e.g. legacy recovery rows) stays a recovered
# extraction -- the original default.
REASON_SOURCE_TYPE = {
    SYLLABUS_REVIEW_REASON: SYLLABUS_SOURCE_TYPE,  # syllabus_derived_first_run -> syllabus_derived
}


def fetch_queue_for_subject(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Queued rows belonging to a subject, via objective_id OR doc_id.

    ingest_review_queue has no subject column, so a row is "for" a subject when
    its objective_id resolves to that subject OR its doc_id does. content_stmt is
    joined in for display context.
    """
    rows = db.execute(
        """
        SELECT q.id, q.source_file, q.chunk_text, q.reason,
               q.objective_id, q.doc_id, q.created_at,
               o.content_stmt AS content_stmt
        FROM   ingest_review_queue q
        LEFT   JOIN objectives o ON o.objective_id = q.objective_id
        LEFT   JOIN documents  d ON d.doc_id       = q.doc_id
        WHERE  o.subject_id = ? OR d.subject_id = ?
        ORDER  BY q.objective_id, q.id
        """,
        (subject_id, subject_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _split_candidate(chunk_text: str) -> tuple[str, str]:
    """Return (point_text, evidence) from a queued candidate string.

    Handles both producers' evidence markers (recovery and syllabus-derived).
    """
    for marker in EVIDENCE_MARKERS:
        if marker in chunk_text:
            point_text, evidence = chunk_text.split(marker, 1)
            return point_text.strip(), evidence.strip()
    return chunk_text.strip(), ""


def _next_point_order(db: sqlite3.Connection, objective_id: str) -> int:
    """One past the current max point_order for an objective (for display ordering)."""
    row = db.execute(
        "SELECT COALESCE(MAX(point_order), 0) AS m FROM mark_points WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    return int(row["m"]) + 1


def promote_row(db: sqlite3.Connection, row: dict) -> str | None:
    """Promote one queued candidate into mark_points, then delete it from the queue.

    Returns the new mark_point_id, or None when the row has no objective_id (it
    can't satisfy the mark_points FK, so it is simply deleted). Idempotent: the
    deterministic id + INSERT OR IGNORE means re-promoting the same text is a no-op.
    """
    objective_id = row.get("objective_id")
    point_text, _evidence = _split_candidate(row.get("chunk_text") or "")

    if not objective_id or not point_text:
        # Nothing valid to promote -- just clear it from the queue.
        db.execute("DELETE FROM ingest_review_queue WHERE id = ?", (row["id"],))
        db.commit()
        return None

    # The promoted point keeps the provenance of whatever pass produced it: a
    # syllabus-derived candidate stays 'syllabus_derived', a recovery candidate
    # (or any unmapped reason) stays 'recovered_extraction'.
    source_type = REASON_SOURCE_TYPE.get(row.get("reason"), SOURCE_TYPE)
    mp_id = _mark_point_id(objective_id, row.get("doc_id") or "", point_text)
    db.execute(
        """
        INSERT OR IGNORE INTO mark_points
            (mark_point_id, objective_id, question_id, doc_id, point_text,
             marks_value, point_order, source_type, source_chunk_id,
             extraction_confidence)
        VALUES (?, ?, NULL, ?, ?, 1, ?, ?, NULL, 100)
        """,
        (mp_id, objective_id, row.get("doc_id"), point_text,
         _next_point_order(db, objective_id), source_type),
    )
    db.execute("DELETE FROM ingest_review_queue WHERE id = ?", (row["id"],))
    db.commit()
    return mp_id


def delete_row(db: sqlite3.Connection, row_id: int) -> None:
    """Reject: remove a queued candidate without writing a mark point."""
    db.execute("DELETE FROM ingest_review_queue WHERE id = ?", (row_id,))
    db.commit()


def _prompt() -> str:
    """Read a single Y/N/Q decision. EOF (piped/empty stdin) is treated as Quit."""
    try:
        return input("  [Y] promote   [N] delete   [Q] quit  > ").strip().lower()
    except EOFError:
        return "q"


def review(db: sqlite3.Connection, subject_id: str) -> dict:
    """Interactive review loop. Returns counts of what happened."""
    ensure_recovery_columns(db)
    queue = fetch_queue_for_subject(db, subject_id)
    counts = {"total": len(queue), "promoted": 0, "deleted": 0, "skipped": 0}

    if not queue:
        print(f"\nNothing queued for review under {subject_id}. Queue is clear.\n")
        return counts

    print(f"\n{len(queue)} item(s) queued for {subject_id}.")
    print("Judge each by FORMAT only -- does it read like a brief CSEC mark point?\n")

    for i, row in enumerate(queue, 1):
        point_text, evidence = _split_candidate(row.get("chunk_text") or "")
        print("-" * 66)
        print(f"Item {i}/{len(queue)}   objective: {row.get('objective_id') or '(none)'}")
        if row.get("content_stmt"):
            print(f"  objective is: {row['content_stmt'][:72]}")
        print(f"  source file : {row.get('source_file') or '(unknown)'}")
        print(f"  reason      : {row.get('reason') or '(none)'}")
        print(f"  CANDIDATE   : {point_text}")
        if evidence:
            print(f"  evidence    : {evidence[:200]}")

        decision = _prompt()
        if decision == "q":
            counts["skipped"] = len(queue) - (i - 1)
            print("\nQuit. Remaining items left in the queue.\n")
            break
        if decision == "y":
            mp_id = promote_row(db, row)
            if mp_id:
                counts["promoted"] += 1
                print(f"  -> promoted to mark_points ({mp_id}).")
            else:
                counts["deleted"] += 1
                print("  -> no valid objective to attach; removed from queue.")
        elif decision == "n":
            delete_row(db, row["id"])
            counts["deleted"] += 1
            print("  -> deleted.")
        else:
            counts["skipped"] += 1
            print("  -> unrecognised input; left in the queue, moving on.")

    print("=" * 66)
    print(f"Reviewed {subject_id}: promoted {counts['promoted']}, "
          f"deleted {counts['deleted']}, left {counts['skipped']}.")
    print("=" * 66)
    return counts


def _open_live_db() -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review and clear the ingest_review_queue for a subject."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    args = parser.parse_args()

    db = _open_live_db()
    try:
        review(db, args.subject)
    finally:
        db.close()


if __name__ == "__main__":
    main()
