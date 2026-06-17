# PHASE: build
"""
backend/db/extract_prose_markpoints.py
======================================
Use the local LLM to turn prose-style worked solutions (the ones with no
bullet list, currently parked in ingest_review_queue) into discrete
`mark_points` the grader can score against.

For each queued chunk we ask MODEL_CHAT to extract the individual ~1-mark
statements as a JSON array of strings, then write each one to mark_points with
a real objective_id (Rule 1: no objective match -> skip, never invent one).

Why no `format` schema (CLAUDE.md "Deterministic vs LLM"):
  The grading *score* is still pure Python -- this script only generates
  candidate mark-point TEXT for later human/automatic grading, so a free-text
  JSON array guided by the system prompt is acceptable. The response is parsed
  defensively: a bad response is logged and skipped, never fatal.

Run (offline-safe; tests only for now):
    python backend/db/extract_prose_markpoints.py --dry-run
    python backend/db/extract_prose_markpoints.py --limit 50
    python backend/db/extract_prose_markpoints.py
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env")

# backend/ on sys.path so `from ollama_client import ...` resolves from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ollama_client import ollama_chat  # noqa: E402

# The queue reason this script consumes. NOTE: the current ingest_solutions.py
# writes prose-answer rows with reason "solution_no_points"; reconcile the two
# (rename here or in the ingester) before running against the live DB.
PROSE_REASON = "prose_answer_no_bullets"

BATCH_SIZE = 20
SLEEP_BETWEEN_BATCHES = 2  # seconds; courtesy pause so we don't hammer the model
DRY_RUN_ROWS = 5

SYSTEM_PROMPT = (
    "You are a CSEC examiner assistant. You will be given a worked solution to a "
    "Principles of Business Paper 2 question. Extract the individual mark points "
    "that a student would need to state to earn marks. Each mark point should be "
    "one clear, concise statement worth approximately 1 mark. Return ONLY a JSON "
    "array of strings. No preamble, no explanation, no markdown. Example output: "
    '["The entrepreneur accepts financial risk.", "A business organises resources '
    'for production."]'
)


# ---------------------------------------------------------------------------
# DB helpers (same pattern as ingest_solutions.py)
# ---------------------------------------------------------------------------
def open_db(db_path: str) -> sqlite3.Connection:
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db = sqlite3.connect(db_path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


# ---------------------------------------------------------------------------
# Response parsing (defensive: never crash on a bad model reply)
# ---------------------------------------------------------------------------
def strip_fences(text: str) -> str:
    """Remove ```json ... ``` (or plain ```) fences a model may wrap output in."""
    s = (text or "").strip()
    s = re.sub(r"^```[A-Za-z0-9_]*\s*\n?", "", s)
    s = re.sub(r"\n?\s*```$", "", s)
    return s.strip()


def parse_points(raw: str) -> list[str]:
    """Parse the model reply into a list of non-empty mark-point strings.

    1. Try json.loads on the whole (fence-stripped) reply.
    2. If that fails, grab the FIRST JSON array in the text and parse that --
       this recovers replies that append a stray sentence after the array.
    3. If that also fails, raise -- the caller logs it and leaves the row in the
       queue (the existing skip behaviour).
    """
    stripped = strip_fences(raw)
    try:
        data = json.loads(stripped)
    except Exception:
        m = re.search(r"\[.*?\]", stripped, re.DOTALL)
        if m is None:
            raise
        data = json.loads(m.group(0))   # may still raise -> caller logs & skips
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    points = [str(x).strip() for x in data if isinstance(x, str) and str(x).strip()]
    if not points:
        raise ValueError("array contained no usable strings")
    return points


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
def fetch_queue(db: sqlite3.Connection, reason: str = PROSE_REASON,
                limit: int | None = None) -> list[dict]:
    sql = ("SELECT id, source_file, chunk_text, objective_id, doc_id "
           "FROM ingest_review_queue WHERE reason = ? ORDER BY id")
    params: list = [reason]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def lookup_objective_and_doc(db: sqlite3.Connection, chunk_text: str) -> tuple[str | None, str | None]:
    """Find the objective_id + doc_id for a queued chunk via its chunks row.

    ingest_review_queue has no objective_id column, so we match the chunk's text
    back to the chunks table. Returns (None, None) if there is no match.
    """
    row = db.execute(
        "SELECT objective_id, doc_id FROM chunks WHERE chunk_text = ? LIMIT 1",
        (chunk_text,),
    ).fetchone()
    if row is None:
        return None, None
    return row["objective_id"], row["doc_id"]


# ---------------------------------------------------------------------------
# Per-row processing
# ---------------------------------------------------------------------------
def process_row(db: sqlite3.Connection, row: dict, chat_fn, dry_run: bool) -> tuple[str, int]:
    """Extract + store mark points for one queued row.

    Returns (outcome, n_points) where outcome is "inserted" | "failed" | "skipped".
      failed  -> model reply was not a usable JSON array (row left in queue)
      skipped -> no objective match for the chunk    (row left in queue, Rule 1)
      inserted-> mark points written (or, in dry-run, would be) and row deleted
    """
    qid = row["id"]
    raw = chat_fn([{"role": "user", "content": row["chunk_text"]}],
                  system=SYSTEM_PROMPT, schema=None)
    try:
        points = parse_points(raw)
    except Exception as exc:  # bad JSON / unexpected shape -> never crash
        print(f"  [fail] queue id {qid}: could not parse model reply ({exc})")
        return "failed", 0

    # Prefer the objective_id / doc_id stored on the queue row itself (the
    # ingester now records them at queue time). Only fall back to the fragile
    # chunk-text lookup when BOTH are missing on the row.
    objective_id = row.get("objective_id")
    doc_id = row.get("doc_id")
    if objective_id is None and doc_id is None:
        objective_id, doc_id = lookup_objective_and_doc(db, row["chunk_text"])
    if objective_id is None:
        print(f"  [skip] queue id {qid}: no objective match for this chunk")
        return "skipped", 0

    if dry_run:
        print(f"  [dry-run] queue id {qid} -> {len(points)} mark point(s) under {objective_id}:")
        for n, pt in enumerate(points, 1):
            print(f"      {objective_id}-prose-{qid}-mp{n}: {pt}")
        return "inserted", len(points)

    for n, pt in enumerate(points, 1):
        db.execute(
            "INSERT OR IGNORE INTO mark_points (mark_point_id, objective_id, "
            "question_id, doc_id, point_text, marks_value, point_order) "
            "VALUES (?, ?, NULL, ?, ?, 1, ?)",
            (f"{objective_id}-prose-{qid}-mp{n}", objective_id, doc_id, pt, n),
        )
    db.execute("DELETE FROM ingest_review_queue WHERE id = ?", (qid,))
    db.commit()
    return "inserted", len(points)


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------
def process_queue(db: sqlite3.Connection, chat_fn=ollama_chat, *, reason: str = PROSE_REASON,
                  limit: int | None = None, dry_run: bool = False,
                  batch_size: int = BATCH_SIZE, sleep_between: int = SLEEP_BETWEEN_BATCHES) -> dict:
    """Process every queued prose row in batches. Returns a summary dict."""
    effective_limit = DRY_RUN_ROWS if dry_run else limit
    rows = fetch_queue(db, reason=reason, limit=effective_limit)

    summary = {"processed": 0, "inserted": 0, "failed": 0, "skipped": 0}
    if not rows:
        print(f"No queued rows with reason {reason!r}.")
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        b_inserted = b_failed = b_skipped = 0
        for row in batch:
            summary["processed"] += 1
            outcome, n = process_row(db, row, chat_fn, dry_run)
            if outcome == "inserted":
                summary["inserted"] += n
                b_inserted += n
            elif outcome == "failed":
                summary["failed"] += 1
                b_failed += 1
            else:
                summary["skipped"] += 1
                b_skipped += 1
        batch_no = start // batch_size + 1
        print(f"Batch {batch_no} complete — {b_inserted} mark points inserted, "
              f"{b_failed} failed, {b_skipped} skipped")
        # Courtesy pause between batches (not after the last, not in dry-run).
        if not dry_run and sleep_between and start + batch_size < len(rows):
            time.sleep(sleep_between)

    remaining = db.execute("SELECT COUNT(1) FROM ingest_review_queue").fetchone()[0]
    summary["remaining"] = remaining
    _print_summary(summary, dry_run)
    return summary


def _print_summary(summary: dict, dry_run: bool) -> None:
    print("\n" + "=" * 60)
    print("Prose mark-point extraction summary" + (" (DRY RUN — nothing written)" if dry_run else ""))
    print("=" * 60)
    print(f"  queued rows processed       : {summary['processed']}")
    print(f"  mark points {'would be inserted' if dry_run else 'inserted     '} : {summary['inserted']}")
    print(f"  rows failed (bad JSON)      : {summary['failed']}")
    print(f"  rows skipped (no objective) : {summary['skipped']}")
    print(f"  rows remaining in queue     : {summary['remaining']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract mark points from prose-style solutions in ingest_review_queue."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help=f"process the first {DRY_RUN_ROWS} rows and print what would be inserted; writes nothing")
    ap.add_argument("--limit", type=int, default=None,
                    help="process only N rows (default: all)")
    args = ap.parse_args()

    db_path = os.getenv("DB_PATH")
    if not db_path or not Path(db_path).exists():
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")

    db = open_db(db_path)
    try:
        process_queue(db, limit=args.limit, dry_run=args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
