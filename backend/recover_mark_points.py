# PHASE: build
"""
backend/recover_mark_points.py
==============================
Stage 8 (Build Playbook v3.1) -- Mark Point Recovery.

A second-pass, LLM-assisted extractor that closes the gap of POB objectives with
NO mark points. The first-pass ingester (ingest.py) used a regex parser that
missed multi-objective questions and non-standard mark-scheme formats; this pass
re-reads the already-ingested mark-scheme chunks and identifies award points that
are ALREADY in the source text.

Non-negotiable constraints (CLAUDE.md + v3.1 "Non-Expert Builder Anchor"):
  * Offline-first. Uses ollama_chat with MODEL_CHAT. NEVER a cloud API -- the
    grading router (Gemini) is deliberately NOT used here.
  * The LLM parses, it does not author. It extracts text already present in the
    chunk; it must never invent generic mark points.
  * Every recovered mark point references a real chunk_id (source_chunk_id) and
    doc_id, so VAL-08 traceability holds.
  * Low-confidence extractions go to ingest_review_queue, never silently into
    mark_points.
  * Fully idempotent: re-running never duplicates a (source_chunk_id, point_text)
    mark point or a queued review row.

Run:
    python backend/recover_mark_points.py --subject Principles_of_Business --dry-run
    python backend/recover_mark_points.py --subject Principles_of_Business
    python backend/recover_mark_points.py --subject Principles_of_Business --min-confidence 80
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

# backend/ on sys.path so the bare module imports resolve whether this is run as
# `python backend/recover_mark_points.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from retrieval import serialize_vec  # noqa: E402
from db.backup import backup_first  # noqa: E402

# The mark-scheme chunks all live in vec_mark_schemes (CLAUDE.md retrieval routing).
VEC_TABLE = "vec_mark_schemes"
DEFAULT_MIN_CONFIDENCE = 70
SEMANTIC_K = 5

SOURCE_TYPE = "recovered_extraction"
REVIEW_REASON = "low_confidence_extraction"


# The model returns one object with a "points" array. Every point must carry a
# verbatim/near-verbatim evidence_quote and a 0-100 confidence, so a low-quality
# chunk (e.g. an OCR'd MCQ answer key) honestly scores itself out of mark_points.
EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["points"],
    "properties": {
        "points": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["point_text", "marks_value", "confidence", "evidence_quote"],
                "properties": {
                    "point_text": {"type": "string"},
                    "marks_value": {"type": "integer"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "evidence_quote": {"type": "string"},
                },
            },
        }
    },
}

EXTRACTION_SYSTEM = (
    "You are a CXC mark-scheme parser. Your ONLY job is to identify discrete award "
    "points that ALREADY appear in the mark-scheme text you are given. You never "
    "author, invent, generalise, or paraphrase beyond the source.\n\n"
    "Rules:\n"
    "- Only extract a point when its wording appears verbatim or near-verbatim in "
    "the chunk. Put the exact phrase you relied on in evidence_quote.\n"
    "- If the chunk does not actually look like a mark scheme (e.g. it is a cover "
    "page, an instruction block, an OCR-garbled multiple-choice answer key, or "
    "unrelated prose), set confidence below 70 for every point you are unsure of, "
    "and prefer returning an empty points array over guessing.\n"
    "- NEVER generate generic, boilerplate, or textbook mark points that are not "
    "in this exact chunk.\n"
    "- marks_value is the marks the point is worth (default 1 if the scheme does "
    "not say).\n"
    "- confidence (0-100) is how certain you are this is a genuine award point "
    "for THIS objective, present in THIS chunk."
)


def ensure_recovery_columns(db: sqlite3.Connection) -> None:
    """Add the Stage 8 provenance columns to mark_points if they are absent.

    Mirrors app.apply_runtime_migrations so the script (and tests) work against a
    DB that has not been opened by the FastAPI app yet. Each ALTER is wrapped
    individually -- a column that already exists raises OperationalError, swallowed
    to stay idempotent.
    """
    for alter in (
        "ALTER TABLE mark_points ADD COLUMN source_type TEXT DEFAULT 'past_paper'",
        "ALTER TABLE mark_points ADD COLUMN source_chunk_id TEXT",
        "ALTER TABLE mark_points ADD COLUMN extraction_confidence INTEGER DEFAULT 100",
    ):
        try:
            db.execute(alter)
        except sqlite3.OperationalError:
            pass
    db.commit()


def objectives_without_mark_points(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Objectives in the subject that currently have ZERO mark points."""
    rows = db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.command_words, o.skill_type
        FROM   objectives o
        WHERE  o.subject_id = ?
          AND  NOT EXISTS (
                SELECT 1 FROM mark_points mp WHERE mp.objective_id = o.objective_id
          )
        ORDER  BY o.objective_id
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def candidate_chunks(db: sqlite3.Connection, subject_id: str, objective: dict,
                     embed_fn=ollama_embed, k: int = SEMANTIC_K) -> list[dict]:
    """Mark-scheme chunks that may hold award points for one objective.

    Two sources, de-duplicated by chunk.id:
      1. Direct -- mark-scheme chunks already tagged to this objective_id.
      2. Semantic -- top-k vec_mark_schemes neighbours of the objective's
         content_stmt, subject-filtered (this is what recovers points the
         first-pass parser missed, including multi-objective questions).
    """
    oid = objective["objective_id"]
    found: dict[int, dict] = {}

    for r in db.execute(
        """
        SELECT c.id, c.chunk_id, c.chunk_text, c.doc_id, c.page, d.source_file
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  c.objective_id = ?
          AND  d.content_type = 'mark_scheme'
        """,
        (subject_id, oid),
    ).fetchall():
        found[r["id"]] = dict(r)

    query = objective.get("content_stmt")
    if query:
        query_vec = serialize_vec(embed_fn(query))
        for r in db.execute(
            f"""
            SELECT c.id, c.chunk_id, c.chunk_text, c.doc_id, c.page,
                   d.source_file, v.distance
            FROM   {VEC_TABLE} v
            JOIN   chunks c    ON c.id = v.rowid
            JOIN   documents d ON d.doc_id = c.doc_id
            WHERE  v.embedding MATCH ?
              AND  k = ?
              AND  v.rowid IN (SELECT id FROM chunks WHERE subject_id = ?)
            ORDER  BY v.distance
            """,
            (query_vec, k, subject_id),
        ).fetchall():
            row = dict(r)
            row.pop("distance", None)
            found.setdefault(row["id"], row)

    # Stable order: tagged/lower-id chunks first, keeps dry-run output deterministic.
    return [found[i] for i in sorted(found)]


def _mark_point_id(objective_id: str, source_chunk_id: str, point_text: str) -> str:
    """Deterministic id so a re-run reproduces the same PK (a second idempotency guard).

    Derived from (source_chunk_id, point_text) -- the same pair the dedup check keys
    on -- so identical extractions always collapse to one row.
    """
    digest = hashlib.sha1(f"{source_chunk_id}|{point_text}".encode("utf-8")).hexdigest()[:10]
    return f"{objective_id}-rec-{digest}"


def _mark_point_exists(db: sqlite3.Connection, source_chunk_id: str, point_text: str) -> bool:
    """True if this exact (source_chunk_id, point_text) is already a mark point."""
    row = db.execute(
        "SELECT 1 FROM mark_points WHERE source_chunk_id = ? AND point_text = ? LIMIT 1",
        (source_chunk_id, point_text),
    ).fetchone()
    return row is not None


def _review_row_exists(db: sqlite3.Connection, objective_id: str, chunk_text: str) -> bool:
    """True if this candidate is already queued for review (re-run dedup)."""
    row = db.execute(
        """
        SELECT 1 FROM ingest_review_queue
        WHERE  objective_id = ? AND chunk_text = ? AND reason = ?
        LIMIT  1
        """,
        (objective_id, chunk_text, REVIEW_REASON),
    ).fetchone()
    return row is not None


def _extract_points(chunk: dict, objective: dict, chat_fn) -> list[dict]:
    """Ask the model for award points present in one chunk. [] on any parse failure."""
    cw = objective.get("command_words") or ""
    user = (
        f"OBJECTIVE ID: {objective['objective_id']}\n"
        f"OBJECTIVE: {objective.get('content_stmt', '')}\n"
        f"COMMAND WORDS: {cw}\n\n"
        f"MARK SCHEME CHUNK (source_chunk_id={chunk['chunk_id']}):\n"
        f"\"\"\"\n{chunk['chunk_text']}\n\"\"\"\n\n"
        "Extract ONLY award points that are actually present in the chunk above. "
        "For each, quote the exact supporting phrase in evidence_quote. If the "
        "chunk is not a usable mark scheme for this objective, return an empty "
        "points array."
    )
    try:
        raw = chat_fn([{"role": "user", "content": user}],
                      system=EXTRACTION_SYSTEM, schema=EXTRACTION_SCHEMA)
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    points = data.get("points")
    return points if isinstance(points, list) else []


def recover_mark_points(db: sqlite3.Connection, subject_id: str, *,
                        min_confidence: int = DEFAULT_MIN_CONFIDENCE,
                        dry_run: bool = False,
                        chat_fn=ollama_chat, embed_fn=ollama_embed,
                        k: int = SEMANTIC_K, verbose: bool = True) -> dict:
    """Run the second-pass recovery for one subject. Returns a summary dict.

    For each zero-mark-point objective: gather candidate mark-scheme chunks, ask
    the model to extract award points from each, then route by confidence --
    >= min_confidence into mark_points (source_type='recovered_extraction'),
    otherwise into ingest_review_queue. Idempotent and, under --dry-run,
    side-effect free.
    """
    ensure_recovery_columns(db)

    objectives = objectives_without_mark_points(db, subject_id)
    summary = {
        "subject_id": subject_id,
        "min_confidence": min_confidence,
        "dry_run": dry_run,
        "objectives_total": len(objectives),
        "objectives_processed": 0,
        "candidate_chunks": 0,
        "points_recovered": 0,
        "points_queued": 0,
        "points_skipped_duplicate": 0,
        "per_objective": [],
    }

    if verbose:
        print(f"\n{len(objectives)} objective(s) in {subject_id} have NO mark points:")
        for o in objectives:
            print(f"  - {o['objective_id']}: {o['content_stmt'][:70]}")
        if dry_run:
            print("\n[DRY RUN] No rows will be written.\n")
        else:
            print()

    for obj in objectives:
        oid = obj["objective_id"]
        chunks = candidate_chunks(db, subject_id, obj, embed_fn=embed_fn, k=k)
        recovered = queued = skipped = 0
        summary["candidate_chunks"] += len(chunks)

        for chunk in chunks:
            for raw_point in _extract_points(chunk, obj, chat_fn):
                point_text = (raw_point.get("point_text") or "").strip()
                if not point_text:
                    continue
                try:
                    confidence = int(raw_point.get("confidence", 0))
                except (TypeError, ValueError):
                    confidence = 0
                try:
                    marks_value = int(raw_point.get("marks_value", 1))
                except (TypeError, ValueError):
                    marks_value = 1
                marks_value = max(1, marks_value)
                evidence = (raw_point.get("evidence_quote") or "").strip()
                source_chunk_id = chunk["chunk_id"]

                if confidence >= min_confidence:
                    if _mark_point_exists(db, source_chunk_id, point_text):
                        skipped += 1
                        continue
                    recovered += 1
                    if verbose:
                        tag = "[DRY] would recover" if dry_run else "recover"
                        print(f"  {oid} <- {tag} (conf {confidence}): {point_text[:60]}")
                    if not dry_run:
                        mp_id = _mark_point_id(oid, source_chunk_id, point_text)
                        db.execute(
                            """
                            INSERT OR IGNORE INTO mark_points
                                (mark_point_id, objective_id, question_id, doc_id,
                                 point_text, marks_value, point_order,
                                 source_type, source_chunk_id, extraction_confidence)
                            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (mp_id, oid, chunk["doc_id"], point_text, marks_value,
                             recovered, SOURCE_TYPE, source_chunk_id, confidence),
                        )
                else:
                    review_text = f"{point_text}\n\nEvidence: {evidence}".strip()
                    if _review_row_exists(db, oid, review_text):
                        skipped += 1
                        continue
                    queued += 1
                    if verbose:
                        tag = "[DRY] would queue" if dry_run else "queue"
                        print(f"  {oid} -> {tag} for review (conf {confidence}): "
                              f"{point_text[:60]}")
                    if not dry_run:
                        db.execute(
                            """
                            INSERT INTO ingest_review_queue
                                (source_file, chunk_text, reason, objective_id, doc_id)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (chunk["source_file"], review_text, REVIEW_REASON,
                             oid, chunk["doc_id"]),
                        )

        summary["objectives_processed"] += 1
        summary["points_recovered"] += recovered
        summary["points_queued"] += queued
        summary["points_skipped_duplicate"] += skipped
        summary["per_objective"].append(
            {"objective_id": oid, "candidate_chunks": len(chunks),
             "recovered": recovered, "queued": queued, "skipped": skipped}
        )

    if not dry_run:
        db.commit()

    # Recount AFTER writes: objectives still empty is the metric the stage closes.
    summary["objectives_still_empty"] = len(
        objectives_without_mark_points(db, subject_id)
    )

    if verbose:
        _print_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    """Human-readable end-of-run table."""
    print("\n" + "=" * 66)
    print(f"Mark Point Recovery -- {summary['subject_id']}"
          f"{'  [DRY RUN]' if summary['dry_run'] else ''}")
    print("=" * 66)
    print(f"  min-confidence threshold   : {summary['min_confidence']}")
    print(f"  objectives with no points  : {summary['objectives_total']}")
    print(f"  objectives processed       : {summary['objectives_processed']}")
    print(f"  candidate chunks examined  : {summary['candidate_chunks']}")
    print(f"  points recovered (>= thr)  : {summary['points_recovered']}")
    print(f"  points queued for review   : {summary['points_queued']}")
    print(f"  duplicates skipped         : {summary['points_skipped_duplicate']}")
    print(f"  objectives STILL empty     : {summary['objectives_still_empty']}")
    print("-" * 66)
    print(f"  {'objective':<14}{'chunks':>8}{'recovered':>11}{'queued':>9}{'skipped':>9}")
    for row in summary["per_objective"]:
        print(f"  {row['objective_id']:<14}{row['candidate_chunks']:>8}"
              f"{row['recovered']:>11}{row['queued']:>9}{row['skipped']:>9}")
    print("=" * 66)
    if summary["dry_run"]:
        print("DRY RUN -- nothing was written. Re-run without --dry-run to apply.")


def _open_live_db() -> sqlite3.Connection:
    """Open the SSD DB the same way the app does (sqlite-vec + FKs)."""
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


@backup_first("pre_recover")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Second-pass LLM-assisted mark-point recovery (offline)."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written; change nothing.")
    parser.add_argument("--min-confidence", type=int, default=DEFAULT_MIN_CONFIDENCE,
                        help=f"Confidence threshold for mark_points "
                             f"(default {DEFAULT_MIN_CONFIDENCE}).")
    args = parser.parse_args()

    db = _open_live_db()
    try:
        recover_mark_points(
            db, args.subject,
            min_confidence=args.min_confidence,
            dry_run=args.dry_run,
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
