# PHASE: build
"""
backend/derive_syllabus_mark_points.py
======================================
Stage 9 (Build Playbook v3.1) -- Syllabus-Derived Mark Points.

The recovery pass (recover_mark_points.py) closes the mark-point gap for
objectives that DO have mark-scheme coverage in the corpus. Some objectives have
none -- no past-paper question, no mark scheme. This pass produces a *fallback*
mark scheme for those objectives, derived strictly from the syllabus
content_stmt plus the subject's own notes/past-paper prose.

Non-negotiable constraints (CLAUDE.md + the recovery pass's anchor):
  * Offline-first. Uses ollama_chat with MODEL_CHAT. NEVER a cloud API.
  * The LLM phrases mark points grounded ONLY in the supplied SOURCE MATERIAL and
    SYLLABUS OBJECTIVE. The prompt forbids introducing any concept, example, or
    terminology absent from those sources.
  * Derived points are second-class evidence. EVERY derived point is *also*
    queued in ingest_review_queue (reason='syllabus_derived_first_run') for
    optional human review -- high confidence does NOT skip the queue.
  * Idempotent: an existing (objective_id, point_text) is skipped, never
    duplicated, and never raises.

Run:
    python backend/derive_syllabus_mark_points.py --subject Principles_of_Business --dry-run
    python backend/derive_syllabus_mark_points.py --subject Principles_of_Business
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
# `python backend/derive_syllabus_mark_points.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from retrieval import serialize_vec  # noqa: E402
# Build-phase routing (PDR v3.1 Section 2.5): cloud MAY fill gaps at build time.
# chat_for_build picks Gemini when CLOUD_MODE=1 (else Ollama); build_engine names
# the engine used, stored as mark_points.source_model. Both are build-only -- this
# script is PHASE: build and never runs on a student path.
from llm_router import chat_for_build, build_engine  # noqa: E402

# Notes are the primary fallback source; past papers backfill when notes are thin.
NOTES_TABLE = "vec_notes"
PAST_PAPERS_TABLE = "vec_past_papers"
SEMANTIC_K = 5
MIN_NOTES_CHUNKS = 2  # below this, also pull from vec_past_papers

SOURCE_TYPE = "syllabus_derived"
REVIEW_REASON = "syllabus_derived_first_run"
REVIEW_SOURCE_FILE = "derive_syllabus_mark_points"
# Evidence marker used inside the queued chunk_text (review_queue.py splits on it).
EVIDENCE_SEP = " | EVIDENCE: "


# One object with a "points" array of 3-5 items. Constrained so the model cannot
# return a single boilerplate point or an unbounded list.
DERIVATION_SCHEMA = {
    "type": "object",
    "required": ["points"],
    "properties": {
        "points": {
            "type": "array",
            "minItems": 3,
            "maxItems": 5,
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

DERIVATION_SYSTEM = (
    "You are extracting mark points for a CSEC examiner mark scheme. "
    "Produce 3 to 5 mark points that an examiner would award for this objective. "
    "Base every point strictly on the SOURCE MATERIAL and SYLLABUS OBJECTIVE above. "
    "Do not introduce any concept, example, or terminology that does not appear in "
    "those sources. Phrase each point as a CSEC mark scheme would: brief, specific, "
    "one idea per point."
)


def ensure_derivation_columns(db: sqlite3.Connection) -> None:
    """Add the provenance columns derived points need, if absent.

    Mirrors app.apply_runtime_migrations so the script (and tests) work against a
    DB that has not been opened by the FastAPI app yet. Each ALTER is wrapped
    individually -- an existing column raises OperationalError, swallowed to stay
    idempotent.
    """
    for alter in (
        "ALTER TABLE mark_points ADD COLUMN source_type TEXT DEFAULT 'past_paper'",
        "ALTER TABLE mark_points ADD COLUMN source_chunk_id TEXT",
        "ALTER TABLE mark_points ADD COLUMN extraction_confidence INTEGER DEFAULT 100",
        "ALTER TABLE mark_points ADD COLUMN command_word TEXT",
        # PDR v3.1: which model authored the point ('gemini' build-time cloud, else
        # 'ollama'). Mirrors app.apply_runtime_migrations so this script works on a
        # DB the FastAPI app has not opened yet.
        "ALTER TABLE mark_points ADD COLUMN source_model TEXT",
    ):
        try:
            db.execute(alter)
        except sqlite3.OperationalError:
            pass
    db.commit()


def objectives_without_mark_points(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Locked-subject objectives that have ZERO mark points of ANY source_type."""
    rows = db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.command_words, o.skill_type
        FROM   objectives o
        JOIN   subjects s ON s.subject_id = o.subject_id
        WHERE  o.subject_id = ?
          AND  s.syllabus_locked = 1
          AND  NOT EXISTS (
                SELECT 1 FROM mark_points mp WHERE mp.objective_id = o.objective_id
          )
        ORDER  BY o.objective_id
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _vec_search(db: sqlite3.Connection, table: str, query_vec: bytes,
                subject_id: str, k: int) -> list[dict]:
    """Top-k subject-filtered neighbours from a vec_* table, joined back to chunks."""
    rows = db.execute(
        f"""
        SELECT c.id, c.chunk_id, c.chunk_text, c.doc_id, c.page, d.source_file
        FROM   {table} v
        JOIN   chunks c    ON c.id = v.rowid
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  v.embedding MATCH ?
          AND  k = ?
          AND  v.rowid IN (SELECT id FROM chunks WHERE subject_id = ?)
        ORDER  BY v.distance
        """,
        (query_vec, k, subject_id),
    ).fetchall()
    return [dict(r) for r in rows]


def candidate_chunks(db: sqlite3.Connection, subject_id: str, objective: dict,
                     embed_fn=ollama_embed, k: int = SEMANTIC_K) -> list[dict]:
    """Source chunks to ground the derivation in, ordered by semantic relevance.

    Top-k from vec_notes on the objective's content_stmt. If fewer than
    MIN_NOTES_CHUNKS notes come back, also pull top-k from vec_past_papers,
    de-duplicated by chunk.id and appended after the notes (notes stay primary).
    """
    query = objective.get("content_stmt")
    if not query:
        return []

    query_vec = serialize_vec(embed_fn(query))
    chunks = _vec_search(db, NOTES_TABLE, query_vec, subject_id, k)

    if len(chunks) < MIN_NOTES_CHUNKS:
        seen = {c["id"] for c in chunks}
        for c in _vec_search(db, PAST_PAPERS_TABLE, query_vec, subject_id, k):
            if c["id"] not in seen:
                chunks.append(c)
                seen.add(c["id"])

    return chunks


def _first_command_word(command_words) -> str | None:
    """Best-effort single command word from the objectives.command_words JSON array."""
    if not command_words:
        return None
    try:
        parsed = json.loads(command_words)
        if isinstance(parsed, list) and parsed:
            return str(parsed[0])
    except (json.JSONDecodeError, TypeError):
        pass
    return str(command_words)


def _mark_point_id(objective_id: str, point_text: str) -> str:
    """Deterministic id keyed on the same (objective_id, point_text) pair the dedup
    check uses, so identical derivations always collapse to one row."""
    digest = hashlib.sha1(f"{objective_id}|{point_text}".encode("utf-8")).hexdigest()[:10]
    return f"{objective_id}-syn-{digest}"


def _mark_point_exists(db: sqlite3.Connection, objective_id: str, point_text: str) -> bool:
    """True if this exact (objective_id, point_text) is already a mark point."""
    row = db.execute(
        "SELECT 1 FROM mark_points WHERE objective_id = ? AND point_text = ? LIMIT 1",
        (objective_id, point_text),
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


def _derive_points(objective: dict, chunks: list[dict], chat_fn) -> list[dict]:
    """Ask the model for 3-5 syllabus-grounded mark points. [] on any parse failure."""
    cw = _first_command_word(objective.get("command_words")) or ""
    source_material = "\n\n".join(
        f"[source_chunk_id={c['chunk_id']}]\n{c['chunk_text']}" for c in chunks
    )
    user = (
        f"SYLLABUS OBJECTIVE:\n{objective.get('content_stmt', '')}\n\n"
        f"COMMAND WORDS:\n{cw}\n\n"
        f"SOURCE MATERIAL:\n{source_material}"
    )
    try:
        raw = chat_fn([{"role": "user", "content": user}],
                      system=DERIVATION_SYSTEM, schema=DERIVATION_SCHEMA)
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        return []
    points = data.get("points")
    return points if isinstance(points, list) else []


def derive_syllabus_mark_points(db: sqlite3.Connection, subject_id: str, *,
                                dry_run: bool = False,
                                chat_fn=None, embed_fn=ollama_embed,
                                k: int = SEMANTIC_K, verbose: bool = True) -> dict:
    """Derive fallback mark points for every zero-mark-point objective in a locked
    subject. Returns a summary dict. Side-effect free under --dry-run.

    chat_fn defaults to the build-phase router (chat_for_build): Gemini when
    CLOUD_MODE=1, else Ollama. The engine actually configured is recorded as
    source_model on every written point. Tests inject chat_fn explicitly.
    """
    ensure_derivation_columns(db)

    # Build-phase provenance (PDR v3.1 Section 2.5). build_engine() reads CLOUD_MODE
    # fresh; it never reaches Gemini in offline mode (short-circuits before the
    # availability check).
    source_model = build_engine()
    if chat_fn is None:
        chat_fn = chat_for_build

    objectives = objectives_without_mark_points(db, subject_id)
    summary = {
        "subject_id": subject_id,
        "dry_run": dry_run,
        "objectives_total": len(objectives),
        "points_written": 0,
        "points_queued": 0,
        "points_skipped_existing": 0,
        "per_objective": [],
    }

    if verbose:
        print(f"\n{len(objectives)} objective(s) in {subject_id} have NO mark points "
              f"of any source_type:")
        for o in objectives:
            print(f"  - {o['objective_id']}: {(o['content_stmt'] or '')[:70]}")
        print("\n[DRY RUN] No rows will be written.\n" if dry_run else "")

    for obj in objectives:
        oid = obj["objective_id"]
        chunks = candidate_chunks(db, subject_id, obj, embed_fn=embed_fn, k=k)
        command_word = _first_command_word(obj.get("command_words"))
        # source_chunk_id = the chunk_id of the primary (first/most-relevant) chunk.
        primary = chunks[0] if chunks else None
        primary_chunk_id = primary["chunk_id"] if primary else None
        primary_doc_id = primary["doc_id"] if primary else None

        written = queued = skipped = 0
        status = "failed"  # no chunks / no points / parse error -> stays 'failed'

        raw_points = _derive_points(obj, chunks, chat_fn) if chunks else []

        for order, raw_point in enumerate(raw_points, 1):
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

            # Idempotency: a point this objective already holds is skipped outright.
            if _mark_point_exists(db, oid, point_text):
                skipped += 1
                continue

            written += 1
            if verbose:
                tag = "[DRY] would derive" if dry_run else "derive"
                print(f"  {oid} <- {tag} (conf {confidence}): {point_text[:60]}")

            if not dry_run:
                mp_id = _mark_point_id(oid, point_text)
                db.execute(
                    """
                    INSERT OR IGNORE INTO mark_points
                        (mark_point_id, objective_id, question_id, doc_id,
                         point_text, marks_value, point_order,
                         source_type, source_chunk_id, extraction_confidence,
                         command_word, source_model)
                    VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mp_id, oid, primary_doc_id, point_text, marks_value, order,
                     SOURCE_TYPE, primary_chunk_id, confidence, command_word,
                     source_model),
                )

            # ALWAYS queue the derived point for optional human review -- high
            # confidence does not skip the queue. Dedup on (objective_id, text).
            review_text = f"{point_text}{EVIDENCE_SEP}{evidence}"
            if not _review_row_exists(db, oid, review_text):
                queued += 1
                if not dry_run:
                    db.execute(
                        """
                        INSERT INTO ingest_review_queue
                            (source_file, chunk_text, reason, objective_id, doc_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (REVIEW_SOURCE_FILE, review_text, REVIEW_REASON,
                         oid, primary_doc_id),
                    )

        if written:
            status = "written"
        elif raw_points and skipped:
            status = "skipped_existing"

        summary["points_written"] += written
        summary["points_queued"] += queued
        summary["points_skipped_existing"] += skipped
        summary["per_objective"].append(
            {"objective_id": oid, "chunks_used": len(chunks),
             "points_written": written, "status": status}
        )

    if not dry_run:
        db.commit()

    if verbose:
        _print_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    """Human-readable end-of-run table (TASK 2f columns)."""
    print("\n" + "=" * 64)
    print(f"Syllabus-Derived Mark Points -- {summary['subject_id']}"
          f"{'  [DRY RUN]' if summary['dry_run'] else ''}")
    print("=" * 64)
    print(f"  objectives with no points : {summary['objectives_total']}")
    print(f"  points written            : {summary['points_written']}")
    print(f"  points queued for review  : {summary['points_queued']}")
    print(f"  points skipped (existing) : {summary['points_skipped_existing']}")
    print("-" * 64)
    print(f"  {'objective_id':<16}{'chunks_used':>12}{'points_written':>16}  status")
    for row in summary["per_objective"]:
        print(f"  {row['objective_id']:<16}{row['chunks_used']:>12}"
              f"{row['points_written']:>16}  {row['status']}")
    print("=" * 64)
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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive fallback mark points from syllabus + notes (offline)."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written; change nothing.")
    args = parser.parse_args()

    db = _open_live_db()
    try:
        derive_syllabus_mark_points(db, args.subject, dry_run=args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
