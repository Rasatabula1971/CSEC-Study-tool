# PHASE: build
"""
backend/upload_ingest.py
========================
Upload session 4 -- turn accepted classifications into real ingestions.

This is the final step of the Upload Material flow
(upload -> extract -> classify -> review -> **ingest** -> regenerate). For each
file a human accepted (or overrode) in session 3, this module:

  * moves the staged file from 06_UPLOAD_STAGING into its KB folder,
  * runs the existing ingest pipeline against it (ingest.ingest_document), passing
    the classification's objectives as a STRONG binding hint (preferred_objectives)
    so Gemini's session-3 work actually steers chunk-objective binding,
  * flags every objective_lessons row whose objective got new chunks as is_stale=1.

Nothing auto-regenerates and nothing auto-ingests -- the user accepts a
classification first, then triggers ingestion, then (separately) chooses which
stale lessons to rebuild. PHASE: build -- never on a student/runtime path.
"""

import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

import ingest  # noqa: E402  -- the existing chunk/embed pipeline (single-file entry)
from ollama_client import ollama_embed  # noqa: E402

STALE_REASON = "new_source_material_added"
# Classification folders that map to an ingestable content type. 00_SYLLABUS /
# 05_STUDENT_WORK / UNCERTAIN have no vec table, so such a file is archived into the
# KB folder but not chunk-embedded.
INGESTABLE_FOLDERS = ingest.FOLDER_CONTENT_TYPE  # folder -> content_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _kb_root() -> str:
    root = os.getenv("KB_ROOT")
    if not root:
        raise IOError("KB_ROOT is not set -- cannot locate the knowledge base.")
    return root


def _fetch_staged(db, staging_id: int):
    """Staging row joined with its classification (or None if the staging row is gone)."""
    return db.execute(
        """
        SELECT s.staging_id, s.subject_id, s.original_name, s.stored_path,
               s.extracted_text, s.truncated, s.ingestion_status,
               c.review_decision, c.recommended_folder, c.review_folder,
               c.objectives_json, c.review_objectives_json
        FROM   upload_staging s
        LEFT   JOIN upload_classifications c ON c.staging_id = s.staging_id
        WHERE  s.staging_id = ?
        """,
        (staging_id,),
    ).fetchone()


def _binding_objectives(row, decision: str) -> list:
    """The objective_ids the file is bound to: the override list for an 'overridden'
    decision (falling back to Gemini's list if the override left objectives unchanged),
    else Gemini's accepted list."""
    raw = None
    if decision == "overridden":
        raw = row["review_objectives_json"] or row["objectives_json"]
    else:
        raw = row["objectives_json"]
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("objective_id"):
            out.append(item["objective_id"])
    return out


def _destination_folder(row, decision: str) -> str:
    """KB sub-folder: the override folder for an 'overridden' decision (falling back to
    the recommended folder if none set), else the recommended folder."""
    if decision == "overridden":
        return row["review_folder"] or row["recommended_folder"]
    return row["recommended_folder"]


def _unique_dest(dest_dir: Path, original_name: str) -> Path:
    """Destination path under dest_dir using the original filename, appending _N before
    the extension if that name already exists (never overwrite an existing KB file)."""
    dest = dest_dir / original_name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        cand = dest_dir / f"{stem}_{n}{suffix}"
        if not cand.exists():
            return cand
        n += 1


def _staged_full_text(db, staging_id: int) -> str:
    """The file's full extracted text: the chunked rows when it was truncated past the
    500k preview cap, else the preview text itself."""
    row = db.execute(
        "SELECT extracted_text, truncated FROM upload_staging WHERE staging_id = ?",
        (staging_id,),
    ).fetchone()
    if row and row["truncated"]:
        chunks = db.execute(
            "SELECT chunk_text FROM upload_staging_chunks WHERE staging_id = ? "
            "ORDER BY chunk_index",
            (staging_id,),
        ).fetchall()
        if chunks:
            return "".join(c["chunk_text"] for c in chunks)
    return (row["extracted_text"] if row else "") or ""


def _stale_matching_lessons(db, objective_ids: list) -> list:
    """Flag every objective_lessons row whose objective just received new chunks as
    stale. Returns the objective_ids actually staled (a lesson existed for them)."""
    staled = []
    for oid in objective_ids:
        cur = db.execute(
            "UPDATE objective_lessons "
            "SET is_stale = 1, stale_reason = ?, staled_at = datetime('now') "
            "WHERE objective_id = ?",
            (STALE_REASON, oid),
        )
        if cur.rowcount and cur.rowcount > 0:
            staled.append(oid)
    return staled


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def ingest_staged_file(db, staging_id: int) -> dict:
    """Move one accepted/overridden staged file into the KB and ingest it.

    Validates the classification decision, copies the file into its KB folder
    (collision-safe), runs ingest.ingest_document with the classification's objectives
    as the binding hint, stales any matching lessons, and records the result in
    ingestion_log + on the staging row. On failure: rolls back DB changes, removes the
    KB copy (the staged file stays in 06_UPLOAD_STAGING -- never a half-move), marks the
    staging row 'failed', logs the error, and re-raises.
    """
    row = _fetch_staged(db, staging_id)
    if row is None:
        raise ValueError(f"No staged file with staging_id={staging_id}.")

    decision = row["review_decision"]
    if decision not in ("accepted", "overridden"):
        raise ValueError(
            f"staging_id={staging_id} is not ingestable: classification "
            f"review_decision={decision!r} (must be accepted or overridden)."
        )
    if row["ingestion_status"] == "ingested":
        raise ValueError(f"staging_id={staging_id} is already ingested.")

    subject_id = row["subject_id"]
    folder = _destination_folder(row, decision)
    if not folder:
        raise ValueError(f"staging_id={staging_id} has no destination folder.")
    binding = _binding_objectives(row, decision)

    src = Path(row["stored_path"])
    dest_dir = Path(_kb_root()) / subject_id / folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_dest(dest_dir, row["original_name"])

    # Mark 'ingesting' so the UI poll reflects in-flight state (committed up front).
    db.execute(
        "UPDATE upload_staging SET ingestion_status = 'ingesting', "
        "updated_at = datetime('now') WHERE staging_id = ?",
        (staging_id,),
    )
    db.commit()

    started = _now()
    shutil.copy2(src, dest)  # copy first; the staged original is removed only on success

    try:
        content_type = INGESTABLE_FOLDERS.get(folder)
        if content_type is None:
            # 00_SYLLABUS / 05_STUDENT_WORK / UNCERTAIN: archive into the KB, no chunks.
            doc_id, chunks_created, objectives_hit = None, 0, []
        else:
            objectives = ingest.load_objectives(db, subject_id)
            result = ingest.ingest_document(
                db, path=dest, subject_id=subject_id, content_type=content_type,
                objectives=objectives, embed_fn=ollama_embed,
                preferred_objectives=binding,
                full_text=_staged_full_text(db, staging_id),
                source_file=str(dest),
            )
            doc_id = result["doc_id"]
            chunks_created = result["chunks_created"]
            objectives_hit = result["objectives_hit"]

        lessons_staled = _stale_matching_lessons(db, objectives_hit)

        db.execute(
            "UPDATE upload_staging SET ingestion_status = 'ingested', "
            "ingested_at = datetime('now'), ingested_doc_id = ?, "
            "ingestion_error = NULL, updated_at = datetime('now') WHERE staging_id = ?",
            (doc_id, staging_id),
        )
        db.execute(
            "INSERT INTO ingestion_log (staging_id, started_at, finished_at, success, "
            "chunks_created, objectives_hit, lessons_staled) "
            "VALUES (?, ?, ?, 1, ?, ?, ?)",
            (staging_id, started, _now(), chunks_created,
             json.dumps(objectives_hit), json.dumps(lessons_staled)),
        )
        db.commit()

        # Move complete -- drop the staged original now that the KB copy is committed.
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass

        return {
            "staging_id": staging_id,
            "doc_id": doc_id,
            "destination": str(dest),
            "chunks_created": chunks_created,
            "objectives_hit": objectives_hit,
            "lessons_staled": lessons_staled,
            "ingestion_status": "ingested",
        }
    except Exception as exc:  # noqa: BLE001 -- recorded, then re-raised
        db.rollback()
        # No half-move: remove the KB copy; the staged original is left in place.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        db.execute(
            "UPDATE upload_staging SET ingestion_status = 'failed', "
            "ingestion_error = ?, updated_at = datetime('now') WHERE staging_id = ?",
            (str(exc)[:1000], staging_id),
        )
        db.execute(
            "INSERT INTO ingestion_log (staging_id, started_at, finished_at, success, "
            "error_message) VALUES (?, ?, ?, 0, ?)",
            (staging_id, started, _now(), str(exc)[:1000]),
        )
        db.commit()
        raise


def ingest_all_accepted(db, subject_id: str, dry_run: bool = False) -> dict:
    """Ingest every accepted/overridden, not-yet-ingested staged file for a subject.

    dry_run reports what WOULD be ingested without copying files or touching the DB.
    """
    rows = db.execute(
        """
        SELECT s.staging_id
        FROM   upload_staging s
        JOIN   upload_classifications c ON c.staging_id = s.staging_id
        WHERE  s.subject_id = ?
          AND  c.review_decision IN ('accepted', 'overridden')
          AND  s.ingestion_status IN ('not_started', 'queued')
        ORDER  BY s.staging_id
        """,
        (subject_id,),
    ).fetchall()
    eligible = [r[0] for r in rows]

    summary = {
        "subject_id": subject_id,
        "dry_run": dry_run,
        "eligible": len(eligible),
        "ingested": 0,
        "failed": 0,
        "skipped": 0,
        "total_chunks": 0,
        "total_objectives_hit": 0,
        "total_lessons_staled": 0,
        "errors": [],
    }
    if dry_run:
        summary["would_ingest"] = eligible
        return summary

    for sid in eligible:
        try:
            res = ingest_staged_file(db, sid)
            summary["ingested"] += 1
            summary["total_chunks"] += res["chunks_created"]
            summary["total_objectives_hit"] += len(res["objectives_hit"])
            summary["total_lessons_staled"] += len(res["lessons_staled"])
        except Exception as exc:  # noqa: BLE001 -- count + record, keep going
            summary["failed"] += 1
            summary["errors"].append({"staging_id": sid, "error": str(exc)})
    return summary


def get_stale_lessons(db, subject_id: str) -> list:
    """Stale lessons for a subject, each with the staged file(s) that caused it (joined
    via ingestion_log.lessons_staled)."""
    lessons = db.execute(
        """
        SELECT l.objective_id, o.content_stmt, l.staled_at, l.stale_reason
        FROM   objective_lessons l
        JOIN   objectives o ON o.objective_id = l.objective_id
        WHERE  l.subject_id = ? AND l.is_stale = 1
        ORDER  BY l.staled_at DESC, l.objective_id
        """,
        (subject_id,),
    ).fetchall()

    # objective_id -> [original_name, ...] from every successful ingestion's staled list.
    cause_map = {}
    log_rows = db.execute(
        "SELECT staging_id, lessons_staled FROM ingestion_log "
        "WHERE lessons_staled IS NOT NULL ORDER BY started_at"
    ).fetchall()
    for lr in log_rows:
        try:
            staled = json.loads(lr["lessons_staled"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not staled:
            continue
        name_row = db.execute(
            "SELECT original_name FROM upload_staging WHERE staging_id = ?",
            (lr["staging_id"],),
        ).fetchone()
        name = name_row["original_name"] if name_row else None
        for oid in staled:
            files = cause_map.setdefault(oid, [])
            if name and name not in files:
                files.append(name)

    return [
        {
            "objective_id": l["objective_id"],
            "content_stmt": l["content_stmt"],
            "staled_at": l["staled_at"],
            "stale_reason": l["stale_reason"],
            "caused_by_files": cause_map.get(l["objective_id"], []),
        }
        for l in lessons
    ]


def regenerate_lessons(db, subject_id: str, objective_ids: list,
                       chat_fn=None, embed_fn=ollama_embed) -> dict:
    """Regenerate the canonical lessons for the given objectives and clear their stale
    flags. Delegates composition to ingest_lessons (offline Ollama by default; tests
    inject chat_fn). is_stale is cleared for every objective the run actually wrote."""
    import ingest_lessons
    summary = ingest_lessons.ingest_lessons_for_subject(
        db, subject_id, regenerate=True, objective_ids=objective_ids,
        chat_fn=chat_fn, embed_fn=embed_fn, verbose=False,
    )
    written = {r["objective_id"] for r in summary.get("rows", [])
               if r.get("status") == "written"}
    cleared = []
    for oid in objective_ids:
        if oid in written:
            db.execute(
                "UPDATE objective_lessons SET is_stale = 0, stale_reason = NULL, "
                "staled_at = NULL WHERE objective_id = ?",
                (oid,),
            )
            cleared.append(oid)
    db.commit()
    return {"summary": summary, "cleared": cleared}
