# PHASE: build
"""
backend/classify_uploads.py
===========================
Upload session 3 -- Gemini classification of staged files.

For each eligible staged file (extract_status='ready', not auto-skipped, not yet
classified), this build-time pass asks the cloud model -- via the PHASE: build
router llm_router.chat_for_classification -- which CSEC objectives the content
covers and which CXC archive folder it belongs in. The proposal is written to
upload_classifications for a human to accept / override / reject in the UI.
Nothing is ingested here; that is session 4.

Non-negotiable constraints (CLAUDE.md + PDR v3.1):
  * PHASE: build. Classification happens once per file at staging time, never on a
    student/runtime path.
  * Routing goes through llm_router.chat_for_classification: Gemini when
    CLOUD_MODE=1 (it knows the POB syllabus), a loud error if Gemini is unreachable,
    Ollama (with a warning) when CLOUD_MODE=0. Never a silent degrade.
  * Rule 1 (every output resolves to a real objective_id): every objective_id the
    model returns is validated against the objectives table and silently dropped if
    it does not exist. The drop count is noted in the rationale.

Run:
    python backend/classify_uploads.py --subject Principles_of_Business --dry-run
    python backend/classify_uploads.py --subject Principles_of_Business
    python backend/classify_uploads.py --subject Principles_of_Business --staging-id 12 --force
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

# backend/ on sys.path so the bare module imports resolve whether this is run as
# `python backend/classify_uploads.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# Build-phase routing (PDR v3.1 Section 2.5). chat_for_classification prefers Gemini
# when CLOUD_MODE=1 and raises if it is unreachable -- it never silently degrades.
from llm_router import chat_for_classification  # noqa: E402
from db.backup import backup_first  # noqa: E402

# How many chars of the extracted text the model sees. The first pages carry the
# title / paper header / first questions, which is what the folder + objective call
# keys on; sending the full body would balloon token cost for little gain.
PROMPT_TEXT_CHARS = 10_000
MAX_OBJECTIVES = 15
# A structured (response_schema) Gemini call is reliable, but the occasional transient
# (a flaky network blip, a rare malformed response) is worth one or two cheap retries
# before recording a file as failed -- a failure costs a manual re-classify later.
CLASSIFY_ATTEMPTS = 3

VALID_FOLDERS = (
    "00_SYLLABUS", "01_SPECIMEN_PAPERS", "02_PAST_PAPERS",
    "03_MARK_SCHEMES", "04_NOTES", "UNCERTAIN",
)

# Schema constrains the local model's output (and asks Gemini for JSON). The prompt
# itself describes the same shape, so a model that ignores the schema still has the
# contract in front of it.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "required": ["recommended_folder", "folder_confidence", "objectives", "rationale"],
    "properties": {
        "recommended_folder": {
            "type": "string",
            "enum": list(VALID_FOLDERS),
        },
        "folder_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "objectives": {
            "type": "array",
            "maxItems": MAX_OBJECTIVES,
            "items": {
                "type": "object",
                "required": ["objective_id", "confidence"],
                "properties": {
                    "objective_id": {"type": "string"},
                    "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                },
            },
        },
        "rationale": {"type": "string"},
    },
}

CLASSIFICATION_SYSTEM = (
    "You are classifying CSEC (Caribbean Secondary Education Certificate) Principles "
    "of Business educational content against the official syllabus.\n\n"
    "For each file given, identify which syllabus objectives the content covers and "
    "recommend which CXC archive folder the file belongs in.\n\n"
    "Be specific. Do not say a textbook covers \"all of POB\". If a chapter teaches "
    "the entrepreneur's role, return only those objectives, not every "
    "entrepreneur-adjacent objective.\n\n"
    "Output strict JSON matching the schema. No preamble. No markdown. No comments."
)


# ---------------------------------------------------------------------------
# Syllabus context
# ---------------------------------------------------------------------------
def load_syllabus_context(db: sqlite3.Connection, subject_id: str) -> tuple:
    """Return (objectives, formatted_text, valid_ids) for a subject.

    objectives: list of dicts (objective_id, objective_num, content_stmt) ordered by
    section then objective. formatted_text: the 'POB-X.Y: content' lines for the
    prompt. valid_ids: the set used to drop any objective_id the model invents.
    """
    rows = db.execute(
        """
        SELECT o.objective_id, o.objective_num, o.content_stmt, o.section_id
        FROM   objectives o
        WHERE  o.subject_id = ?
        ORDER  BY o.section_id, o.objective_num
        """,
        (subject_id,),
    ).fetchall()
    objectives = [dict(r) for r in rows]
    valid_ids = {o["objective_id"] for o in objectives}
    lines = [f"{o['objective_id']}: {(o['content_stmt'] or '').strip()}" for o in objectives]
    return objectives, "\n".join(lines), valid_ids


def _eligible_files(db: sqlite3.Connection, subject_id: str, *,
                    staging_id=None, force: bool = False) -> list:
    """Staged files this run will send to the model.

    extract_status='ready' AND skip_classification=0 AND (classification_status is
    'unclassified' OR force). A single staging_id narrows to that one file.
    """
    sql = (
        "SELECT staging_id, subject_id, original_name, file_type, ocr_used, "
        "       extracted_text, classification_status "
        "FROM   upload_staging "
        "WHERE  subject_id = ? AND extract_status = 'ready' "
        "  AND  skip_classification = 0 "
    )
    params = [subject_id]
    if not force:
        sql += "  AND classification_status = 'unclassified' "
    if staging_id is not None:
        sql += "  AND staging_id = ? "
        params.append(staging_id)
    sql += "ORDER BY staging_id"
    return [dict(r) for r in db.execute(sql, params).fetchall()]


def _build_user_prompt(syllabus_text: str, file_row: dict) -> str:
    """The USER message: full syllabus + this file's metadata and first chars."""
    text = (file_row.get("extracted_text") or "")[:PROMPT_TEXT_CHARS]
    ocr = "true" if file_row.get("ocr_used") else "false"
    return (
        "SYLLABUS (POB objectives -- id, content):\n"
        f"{syllabus_text}\n\n"
        f"FILE NAME: {file_row.get('original_name')}\n"
        f"FILE TYPE: {file_row.get('file_type')}\n"
        f"OCR USED: {ocr}\n"
        f"FIRST {PROMPT_TEXT_CHARS} CHARS:\n"
        f"{text}\n\n"
        "Respond with this JSON shape:\n"
        "{\n"
        '  "recommended_folder": '
        '"00_SYLLABUS|01_SPECIMEN_PAPERS|02_PAST_PAPERS|03_MARK_SCHEMES|04_NOTES|UNCERTAIN",\n'
        '  "folder_confidence": 0-100,\n'
        '  "objectives": [ {"objective_id": "POB-X.Y", "confidence": 0-100} ],\n'
        '  "rationale": "One sentence explaining the classification."\n'
        "}\n\n"
        "Rules:\n"
        "- objectives list at most 15 items.\n"
        "- Only include objectives with confidence >= 50.\n"
        "- If you cannot determine the folder, use \"UNCERTAIN\" with "
        "folder_confidence < 50.\n"
        "- If you cannot identify any objectives, return empty array."
    )


def _extract_json(text: str) -> dict:
    """Parse a model response into a dict, tolerating the common ways a cloud model
    wraps JSON despite being told not to: a ```json fence, or prose before/after the
    object. Falls back to the outermost {...} slice. Raises json.JSONDecodeError if no
    valid object can be recovered (the caller records that as a failed classification)."""
    s = (text or "").strip()
    if s.startswith("```"):
        # ```json\n{...}\n```  ->  drop the fences
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            return json.loads(s[start:end + 1])
        raise


def _clean_classification(raw: dict, valid_ids: set) -> dict:
    """Validate + normalise a model response. Drops invented objective_ids, caps the
    list at 15, clamps confidences, and notes any drop in the rationale. Returns the
    fields the upload_classifications row needs."""
    folder = raw.get("recommended_folder")
    if folder not in VALID_FOLDERS:
        folder = "UNCERTAIN"
    try:
        folder_conf = max(0, min(100, int(raw.get("folder_confidence", 0))))
    except (TypeError, ValueError):
        folder_conf = 0

    kept, dropped = [], 0
    for item in (raw.get("objectives") or []):
        oid = (item or {}).get("objective_id")
        if oid in valid_ids:
            try:
                conf = max(0, min(100, int(item.get("confidence", 0))))
            except (TypeError, ValueError):
                conf = 0
            kept.append({"objective_id": oid, "confidence": conf})
        else:
            dropped += 1
    kept = kept[:MAX_OBJECTIVES]

    rationale = (raw.get("rationale") or "").strip()
    if dropped:
        note = (f"[Filtered {dropped} objective id(s) not in the syllabus.]")
        rationale = f"{rationale} {note}".strip()

    return {
        "recommended_folder": folder,
        "folder_confidence": folder_conf,
        "objectives": kept,
        "rationale": rationale,
        "dropped": dropped,
    }


def _write_classification(db: sqlite3.Connection, staging_id: int, model_used: str,
                          cleaned: dict, raw_response: str) -> None:
    """Upsert one upload_classifications row (staging_id is UNIQUE). INSERT OR REPLACE
    re-classifies cleanly under --force, clearing any prior review decision."""
    db.execute(
        """
        INSERT OR REPLACE INTO upload_classifications
            (staging_id, recommended_folder, folder_confidence, objectives_json,
             rationale, model_used, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (staging_id, cleaned["recommended_folder"], cleaned["folder_confidence"],
         json.dumps(cleaned["objectives"]), cleaned["rationale"], model_used,
         raw_response),
    )


def _set_status(db: sqlite3.Connection, staging_id: int, status: str) -> None:
    db.execute(
        "UPDATE upload_staging SET classification_status = ?, "
        "updated_at = datetime('now') WHERE staging_id = ?",
        (status, staging_id),
    )


def classify_uploads(db: sqlite3.Connection, subject_id: str, *,
                     staging_id=None, force: bool = False, dry_run: bool = False,
                     chat_fn=None, verbose: bool = True) -> dict:
    """Classify every eligible staged file in a subject (or one, with staging_id).

    chat_fn defaults to llm_router.chat_for_classification (Gemini when CLOUD_MODE=1,
    Ollama otherwise). Tests inject chat_fn. Returns a summary dict; side-effect free
    under --dry-run.
    """
    if chat_fn is None:
        chat_fn = chat_for_classification
    # What chat_for_classification will actually have used on success: Gemini when
    # CLOUD_MODE=1 (it raises rather than fall back), else Ollama.
    model_used = "gemini" if os.getenv("CLOUD_MODE", "0") == "1" else "ollama"

    _objectives, syllabus_text, valid_ids = load_syllabus_context(db, subject_id)

    # Whole-subject runs only -- a single-file run must not touch the rest.
    if staging_id is None and not dry_run:
        # Self-heal: a previous run interrupted mid-flight can leave a row stuck in
        # 'classifying'/'queued'; reset it so this run picks it up again.
        db.execute(
            "UPDATE upload_staging SET classification_status = 'unclassified' "
            "WHERE subject_id = ? AND classification_status IN ('classifying','queued')",
            (subject_id,),
        )
        # Mark auto-skipped files 'skipped' (no model call).
        db.execute(
            "UPDATE upload_staging SET classification_status = 'skipped' "
            "WHERE subject_id = ? AND skip_classification = 1 "
            "  AND classification_status != 'skipped'",
            (subject_id,),
        )
        db.commit()

    files = _eligible_files(db, subject_id, staging_id=staging_id, force=force)
    summary = {
        "subject_id": subject_id,
        "dry_run": dry_run,
        "model_used": model_used,
        "eligible": len(files),
        "classified": 0,
        "failed": 0,
        "rows": [],
    }

    if verbose:
        print(f"\n{len(files)} eligible file(s) in {subject_id} to classify "
              f"(model: {model_used}){'  [DRY RUN]' if dry_run else ''}.")

    for f in files:
        sid = f["staging_id"]
        if not dry_run:
            _set_status(db, sid, "classifying")
            db.commit()

        user = _build_user_prompt(syllabus_text, f)
        raw_response = None  # last response seen, for the audit row on total failure
        cleaned, exc = None, None
        for attempt in range(CLASSIFY_ATTEMPTS):
            try:
                raw_response = chat_fn(
                    [{"role": "user", "content": user}],
                    system=CLASSIFICATION_SYSTEM, schema=CLASSIFICATION_SCHEMA,
                )
                cleaned = _clean_classification(_extract_json(raw_response), valid_ids)
                exc = None
                break
            except Exception as e:  # noqa: BLE001 -- retry, then record as failed
                exc = e
                if verbose and attempt + 1 < CLASSIFY_ATTEMPTS:
                    print(f"  staging {sid}: attempt {attempt + 1} failed ({e}); retrying")

        if exc is not None:  # all attempts exhausted -- record a failed classification
            summary["failed"] += 1
            summary["rows"].append({
                "staging_id": sid, "original_name": f["original_name"],
                "folder": "-", "top": [], "status": "failed",
            })
            if verbose:
                print(f"  staging {sid} ({f['original_name']}): FAILED -- {exc}")
            if not dry_run:
                # Record the failure in the classification table (no error column on
                # upload_staging): empty objectives + an ERROR rationale.
                _write_classification(
                    db, sid, model_used,
                    {"recommended_folder": "UNCERTAIN", "folder_confidence": 0,
                     "objectives": [], "rationale": f"ERROR: {exc}"},
                    raw_response,
                )
                _set_status(db, sid, "failed")
                db.commit()
            continue

        top = [o["objective_id"] for o in cleaned["objectives"][:3]]
        summary["classified"] += 1
        summary["rows"].append({
            "staging_id": sid, "original_name": f["original_name"],
            "folder": cleaned["recommended_folder"], "top": top, "status": "classified",
        })
        if verbose:
            drop = f" (-{cleaned['dropped']} filtered)" if cleaned["dropped"] else ""
            tag = "[DRY] would classify" if dry_run else "classified"
            print(f"  staging {sid} ({f['original_name']}): {tag} -> "
                  f"{cleaned['recommended_folder']} ({cleaned['folder_confidence']}%), "
                  f"objectives {top}{drop}")

        if not dry_run:
            _write_classification(db, sid, model_used, cleaned, raw_response)
            _set_status(db, sid, "classified")
            db.commit()

    if verbose:
        _print_summary(summary)
    return summary


def _print_summary(summary: dict) -> None:
    """End-of-run table (TASK 2g columns)."""
    print("\n" + "=" * 72)
    print(f"Upload classification -- {summary['subject_id']}"
          f"  (model: {summary['model_used']})"
          f"{'  [DRY RUN]' if summary['dry_run'] else ''}")
    print("=" * 72)
    print(f"  eligible   : {summary['eligible']}")
    print(f"  classified : {summary['classified']}")
    print(f"  failed     : {summary['failed']}")
    print("-" * 72)
    print(f"  {'id':>4}  {'folder':<18}  {'top objectives':<28}  name")
    for r in summary["rows"]:
        top = ", ".join(r["top"]) or "-"
        print(f"  {r['staging_id']:>4}  {r['folder']:<18}  {top[:28]:<28}  "
              f"{r['original_name'][:40]}")
    print("=" * 72)
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


@backup_first("pre_classification")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Classify staged upload files against the syllabus (Gemini at build time)."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    parser.add_argument("--staging-id", type=int, default=None,
                        help="Classify only this one staged file.")
    parser.add_argument("--force", action="store_true",
                        help="Re-classify files that are already classified.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the model, print the proposal, write nothing.")
    args = parser.parse_args()

    db = _open_live_db()
    try:
        classify_uploads(db, args.subject, staging_id=args.staging_id,
                         force=args.force, dry_run=args.dry_run)
    finally:
        db.close()


if __name__ == "__main__":
    main()
