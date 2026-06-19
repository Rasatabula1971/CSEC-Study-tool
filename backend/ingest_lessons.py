# PHASE: build
"""
backend/ingest_lessons.py
=========================
Stage 11 (Build Playbook v3.1) -- Canonical Lessons.

Pre-generate ONE canonical lesson per syllabus objective at build time, store it
in objective_lessons, and let the runtime teach route serve it deterministically
(no Ollama call on a student request). This eliminates topical drift and removes
the regex-based active-recall extraction the UI used to do client-side.

Non-negotiable constraints (CLAUDE.md + the v3.1 non-expert-builder anchor):
  * Offline build step. Composes with ollama_chat / MODEL_CHAT. CLOUD_MODE has no
    effect here -- this is offline source-grounded composition, not a gap-fill.
  * The model REWRITES the supplied SOURCE MATERIAL for a Form 5 student. It never
    invents concepts, examples, or terminology absent from the source chunks.
  * No source, no lesson. An objective with zero source chunks is queued in
    lesson_generation_queue (reason='insufficient_sources'), never written blind.
  * Confidence is floored locally -- the model's self-reported confidence is
    capped by a floor derived from how much real source material was available.
  * Idempotent. A lesson already present is skipped unless --regenerate is set,
    which DELETEs the existing row before writing the new one.

Run:
    python backend/ingest_lessons.py --subject Principles_of_Business
    python backend/ingest_lessons.py --subject Principles_of_Business --regenerate
    python backend/ingest_lessons.py --subject Principles_of_Business --confidence-floor 30
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# backend/ on sys.path so the bare module imports resolve whether this is run as
# `python backend/ingest_lessons.py` or imported in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from ollama_client import ollama_chat, ollama_embed  # noqa: E402
from retrieval import serialize_vec  # noqa: E402
from db.backup import backup_first  # noqa: E402

# Notes are the primary source. When fewer than MIN_NOTES_CHUNKS come back the
# composer also pulls past papers and mark schemes so the lesson still has
# something concrete to rewrite (it just lowers the confidence floor).
NOTES_TABLE = "vec_notes"
PAST_PAPERS_TABLE = "vec_past_papers"
MARK_SCHEMES_TABLE = "vec_mark_schemes"
NOTES_K = 5
FALLBACK_K = 3
MIN_NOTES_CHUNKS = 2

# Short table -> display source name, used in the summary "sources" column.
SOURCE_NAMES = {
    NOTES_TABLE: "notes",
    PAST_PAPERS_TABLE: "papers",
    MARK_SCHEMES_TABLE: "schemes",
}

DEFAULT_CONFIDENCE_FLOOR = 30
QUEUE_REASON = "insufficient_sources"


# One object: the full lesson. recall_questions is pinned to exactly 3; the model
# cannot return a single boilerplate question or an unbounded list.
LESSON_SCHEMA = {
    "type": "object",
    "required": ["lesson_text", "key_terms", "worked_examples",
                 "common_mistakes", "recall_questions", "confidence"],
    "properties": {
        "lesson_text": {"type": "string"},
        "key_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["term", "definition"],
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                },
            },
        },
        "worked_examples": {
            "type": "array",
            "items": {"type": "string"},
        },
        "common_mistakes": {"type": "string"},
        "recall_questions": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": {"type": "string"},
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
        },
    },
}

LESSON_SYSTEM = (
    "You are writing a study lesson for a CSEC Form 5 student aged\n"
    "15-16. Your job is to rewrite the provided source material into\n"
    "a clear, simple lesson. You are not generating new content; you\n"
    "are recomposing what is already in the SOURCE MATERIAL section\n"
    "below.\n\n"
    "STRICT RULES:\n"
    "- Keep every factual claim that appears in the source material.\n"
    "- Do not introduce concepts, examples, terminology, or facts\n"
    "  that do not appear in the source material.\n"
    "- Use short sentences. Aim for 200-350 words in lesson_text.\n"
    "- Avoid jargon unless it appears in the source material; when\n"
    "  used, define it the first time.\n"
    "- Produce exactly 3 active-recall questions that an examiner\n"
    "  could ask on this objective. Questions must require the\n"
    "  student to recall or apply, not just recognise.\n"
    "- common_mistakes: 1-2 sentences naming what examiners often\n"
    "  penalise on this kind of question.\n"
    "- worked_examples: 0-3 worked examples drawn from the source.\n\n"
    "CRITICAL OUTPUT RULES:\n"
    "- Recall questions must be complete questions ending in '?'.\n"
    "- Never write 'let me know', 'feel free to ask', 'clarification',\n"
    "  or any conversational closing in any field.\n"
    "- Never cite chunk section numbers as if they are authoritative\n"
    "  ('According to Section 2'). The chunks are source material, not\n"
    "  citable references. Refer to concepts directly.\n"
    "- Each recall question must require specific recall or explanation,\n"
    "  not generic placeholders like 'Explain objective X'."
)


# A recall prompt is valid if it ENDS in '?' or BEGINS with one of these CSEC command
# words (imperative prompts like "Identify one example..." are real recall questions
# that do not end in '?'). Junk array elements ('multiple-choice', a leaked answer in
# parentheses) match neither and are rejected.
_RECALL_COMMAND_WORDS = (
    "define", "state", "explain", "identify", "describe", "discuss",
    "distinguish", "outline", "list", "calculate", "compare", "contrast",
    "name", "give",
)


def _validate_lesson_quality(lesson_text, recall_questions):
    """Reject semantically-broken lessons the JSON schema can't catch (chat
    boilerplate, hallucinated 'Section N' citations, junk recall questions).

    Returns (ok, reason): (True, None) when the lesson is clean, else (False, reason).
    Applied before INSERT so a syntactically-valid-but-broken lesson is queued for a
    re-attempt instead of being served to a student.

    A recall question is accepted when it ends in '?' OR opens with a CSEC command
    word -- valid imperative prompts ("Identify two roles of...") do not end in '?',
    so requiring a literal '?' would wrongly reject good questions while still letting
    junk through. Everything else (boilerplate, too-short, non-string, leaked answers,
    bare labels like 'multiple-choice') is rejected.
    """
    lower = (lesson_text or "").lower()
    if 'according to section' in lower:
        return False, 'lesson_text cites chunk section'
    if 'let me know' in lower or 'feel free' in lower or 'clarification' in lower:
        return False, 'lesson_text contains chat boilerplate'
    if not isinstance(recall_questions, list) or len(recall_questions) != 3:
        got = len(recall_questions) if isinstance(recall_questions, list) else 'non-list'
        return False, f'recall_questions count != 3 (got {got})'
    for q in recall_questions:
        if not isinstance(q, str):
            return False, 'non-string in recall_questions'
        qs = q.strip()
        if len(qs) < 15:
            return False, 'recall_question too short'
        ql = qs.lower()
        if 'let me know' in ql or 'feel free' in ql or 'clarification' in ql:
            return False, 'recall_question contains chat boilerplate'
        # Answer leakage: the model appended the answer in parentheses, e.g.
        # "What is the term...? (Answer: Transportation)" (the POB-10.13 pattern).
        if '(answer:' in ql:
            return False, 'recall_question leaks the answer'
        is_question = qs.endswith('?')
        is_command = any(ql.startswith(cw + ' ') for cw in _RECALL_COMMAND_WORDS)
        if not (is_question or is_command):
            return False, 'recall_question is not a question or command prompt'
    return True, None


def ensure_lesson_tables(db: sqlite3.Connection) -> None:
    """Create objective_lessons + lesson_generation_queue if absent.

    Mirrors app.apply_runtime_migrations so the script (and tests) work against a
    DB the FastAPI app has not opened yet. CREATE TABLE IF NOT EXISTS is a no-op
    when the table already exists.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS objective_lessons (
            lesson_id          TEXT PRIMARY KEY,
            objective_id       TEXT NOT NULL UNIQUE REFERENCES objectives(objective_id),
            subject_id         TEXT NOT NULL REFERENCES subjects(subject_id),
            lesson_text        TEXT NOT NULL,
            worked_examples    TEXT,
            key_terms          TEXT,
            common_mistakes    TEXT,
            recall_questions   TEXT NOT NULL,
            source_chunk_ids   TEXT NOT NULL,
            confidence         INTEGER NOT NULL,
            generated_at       TEXT DEFAULT (datetime('now')),
            reviewed           INTEGER DEFAULT 0
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS lesson_generation_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id  TEXT NOT NULL,
            reason        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
        """
    )
    # UNIQUE(objective_id, reason) backs the idempotent upsert in
    # _queue_insufficient. Try/except: if the live DB still holds duplicate pairs
    # the CREATE fails -- the one-off cleanup dedupes first, then this succeeds.
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lgq_objective_reason "
            "ON lesson_generation_queue(objective_id, reason)"
        )
    except sqlite3.OperationalError:
        pass
    db.commit()


def locked_subject_objectives(db: sqlite3.Connection, subject_id: str) -> list[dict]:
    """Every objective in a LOCKED subject, ordered by id."""
    rows = db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.command_words, o.skill_type
        FROM   objectives o
        JOIN   subjects s ON s.subject_id = o.subject_id
        WHERE  o.subject_id = ?
          AND  s.syllabus_locked = 1
        ORDER  BY o.objective_id
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _lesson_exists(db: sqlite3.Connection, objective_id: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM objective_lessons WHERE objective_id = ? LIMIT 1",
        (objective_id,),
    ).fetchone()
    return row is not None


def _vec_search(db: sqlite3.Connection, table: str, query_vec: bytes,
                subject_id: str, k: int) -> list[dict]:
    """Top-k subject-filtered neighbours from a vec_* table, joined back to chunks.

    Each returned chunk is tagged with its source vec table so the caller can count
    notes vs papers vs schemes for the confidence floor. NOTE the `AND k = ?` form:
    sqlite-vec kNN with a JOIN needs the k constraint, not LIMIT (v0.1.9+).
    """
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
    out = []
    for r in rows:
        d = dict(r)
        d["vec_table"] = table
        out.append(d)
    return out


def candidate_chunks(db: sqlite3.Connection, subject_id: str, objective: dict,
                     embed_fn=ollama_embed) -> list[dict]:
    """Source chunks to ground the lesson in, ordered notes-first.

    Top-5 from vec_notes on the objective's content_stmt. If fewer than
    MIN_NOTES_CHUNKS notes come back, also pull top-3 from vec_past_papers AND
    vec_mark_schemes, de-duplicated by chunk.id and appended after the notes
    (notes stay primary). Returns [] when the objective has no content_stmt.
    """
    query = objective.get("content_stmt")
    if not query:
        return []

    query_vec = serialize_vec(embed_fn(query))
    chunks = _vec_search(db, NOTES_TABLE, query_vec, subject_id, NOTES_K)

    if len(chunks) < MIN_NOTES_CHUNKS:
        seen = {c["id"] for c in chunks}
        for table in (PAST_PAPERS_TABLE, MARK_SCHEMES_TABLE):
            for c in _vec_search(db, table, query_vec, subject_id, FALLBACK_K):
                if c["id"] not in seen:
                    chunks.append(c)
                    seen.add(c["id"])
    return chunks


def _command_word_list(command_words) -> str:
    """JSON command_words array -> comma list for the prompt; '' when absent."""
    if not command_words:
        return ""
    try:
        parsed = json.loads(command_words)
        if isinstance(parsed, list):
            return ", ".join(str(c) for c in parsed)
    except (json.JSONDecodeError, TypeError):
        pass
    return str(command_words)


def local_confidence_floor(chunks: list[dict]) -> int:
    """Cap derived from how much REAL source material backed the lesson.

    More notes -> a higher floor. With no notes and only mark schemes used, drop
    20 (a mark scheme is the weakest base for a lesson). Never below 30, so the
    floor only ever LOWERS an over-confident model, never raises a poor lesson.
    """
    notes_used = sum(1 for c in chunks if c["vec_table"] == NOTES_TABLE)
    papers_used = sum(1 for c in chunks if c["vec_table"] == PAST_PAPERS_TABLE)
    schemes_used = sum(1 for c in chunks if c["vec_table"] == MARK_SCHEMES_TABLE)

    if notes_used >= 3:
        base = 90
    elif notes_used == 2:
        base = 70
    elif notes_used == 1:
        base = 50
    else:
        base = 30

    if notes_used == 0 and papers_used == 0 and schemes_used > 0:
        base -= 20

    return max(base, 30)


def _compose_lesson(objective: dict, chunks: list[dict], chat_fn) -> dict | None:
    """Ask the model to compose the lesson from the source chunks. None on parse failure."""
    cw = _command_word_list(objective.get("command_words"))
    skill = objective.get("skill_type") or "(unspecified)"
    source_material = "\n---\n".join(c["chunk_text"] for c in chunks)
    user = (
        f"SYLLABUS OBJECTIVE: {objective.get('content_stmt', '')}\n"
        f"COMMAND WORDS: {cw}\n"
        f"SKILL TYPE: {skill}\n\n"
        f"SOURCE MATERIAL:\n{source_material}\n\n"
        "Respond ONLY with a valid JSON object matching the schema."
    )
    try:
        raw = chat_fn([{"role": "user", "content": user}],
                      system=LESSON_SYSTEM, schema=LESSON_SCHEMA)
        data = json.loads(raw)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _queue_insufficient(db: sqlite3.Connection, objective_id: str,
                        dry_run: bool) -> None:
    """Idempotently flag an objective as needing sources.

    ON CONFLICT(objective_id, reason) means a re-run REFRESHES created_at on the
    existing row rather than stacking a duplicate -- so the queue stays one row per
    objective-per-reason of genuine outstanding work.
    """
    if not dry_run:
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason) "
            "VALUES (?, ?) "
            "ON CONFLICT(objective_id, reason) DO UPDATE SET created_at = datetime('now')",
            (objective_id, QUEUE_REASON),
        )


def _queue_quality_failed(db: sqlite3.Connection, objective_id: str,
                          reason: str, dry_run: bool) -> None:
    """Queue an objective whose composed lesson failed the quality gate, so it is
    re-attempted later instead of a broken lesson being written. The specific failure
    is folded into the reason ('quality_check_failed: <why>')."""
    if not dry_run:
        full_reason = "quality_check_failed: " + (reason or "unknown")
        db.execute(
            "INSERT INTO lesson_generation_queue (objective_id, reason) "
            "VALUES (?, ?) "
            "ON CONFLICT(objective_id, reason) DO UPDATE SET created_at = datetime('now')",
            (objective_id, full_reason),
        )


def ingest_lessons_for_subject(db: sqlite3.Connection, subject_id: str, *,
                               regenerate: bool = False,
                               confidence_floor: int = DEFAULT_CONFIDENCE_FLOOR,
                               chat_fn=None, embed_fn=ollama_embed,
                               dry_run: bool = False, verbose: bool = True,
                               objective_ids: list[str] | None = None) -> dict:
    """Compose canonical lessons for every objective in a locked subject.

    chat_fn defaults to local Ollama (offline build composition). Tests inject it.
    `objective_ids`, when given, restricts the run to those objectives (session 4:
    regenerate only the stale lessons the user asked for). Returns a summary dict.
    Side-effect free under dry_run.
    """
    ensure_lesson_tables(db)
    if chat_fn is None:
        chat_fn = ollama_chat

    objectives = locked_subject_objectives(db, subject_id)
    if objective_ids is not None:
        wanted = set(objective_ids)
        objectives = [o for o in objectives if o["objective_id"] in wanted]
    summary = {
        "subject_id": subject_id,
        "regenerate": regenerate,
        "confidence_floor": confidence_floor,
        "objectives_total": len(objectives),
        "written": 0,
        "queued": 0,
        "skipped": 0,
        "errored": 0,
        "cleared": 0,  # stale queue rows deleted when a lesson was successfully written
        "rows": [],
    }

    if verbose:
        print(f"\nCanonical lessons -- {subject_id} "
              f"({len(objectives)} objective(s) in a locked subject)"
              f"{'  [DRY RUN]' if dry_run else ''}\n")
        print(f"  {'objective_id':<16}{'chunks':>7}  {'sources':<18}"
              f"{'conf':>4}{'cleared':>8}  status")
        print("  " + "-" * 68)

    for obj in objectives:
        oid = obj["objective_id"]
        # Commit this objective's net change before moving on (try/finally below), so
        # a later failure -- e.g. a single Ollama ReadTimeout deep in the subject --
        # never rolls back the lessons already written. A re-run skips what landed.
        try:
            # (a) An existing lesson is skipped unless --regenerate. The actual DELETE
            # is deferred to the write path (f) so a compose failure on a regenerate
            # cannot destroy a good lesson and leave nothing in its place.
            if _lesson_exists(db, oid) and not regenerate:
                _record(summary, oid, 0, "", None, "skipped_exists", verbose)
                summary["skipped"] += 1
                continue

            # (b) Retrieve source chunks (notes first, fallback to papers + schemes).
            try:
                chunks = candidate_chunks(db, subject_id, obj, embed_fn=embed_fn)
            except Exception:  # embedding/search failure for one objective is non-fatal
                _record(summary, oid, 0, "", None, "errored", verbose)
                summary["errored"] += 1
                continue

            sources = _source_label(chunks)

            # No source, no lesson (Stage 11 constraint): queue, do not compose blind.
            if not chunks:
                _queue_insufficient(db, oid, dry_run)
                _record(summary, oid, 0, sources, None, "queued", verbose)
                summary["queued"] += 1
                continue

            # (c)/(d) Compose from the source material. A network/timeout error on ONE
            # objective must not abort the whole subject pass -- record it and move on.
            try:
                data = _compose_lesson(obj, chunks, chat_fn)
            except Exception:
                data = None
            if data is None:
                _record(summary, oid, len(chunks), sources, None, "errored", verbose)
                summary["errored"] += 1
                continue

            # (e) Confidence: model self-report capped by the local floor.
            try:
                model_conf = int(data.get("confidence", 0))
            except (TypeError, ValueError):
                model_conf = 0
            floor = local_confidence_floor(chunks)
            final_conf = min(model_conf, floor)

            if final_conf < confidence_floor:
                _queue_insufficient(db, oid, dry_run)
                _record(summary, oid, len(chunks), sources, final_conf, "queued", verbose)
                summary["queued"] += 1
                continue

            # (e2) Quality gate: reject semantically-broken output the JSON schema
            # cannot catch (chat boilerplate, 'According to Section N' citations, junk
            # recall questions). A failure is queued for re-attempt, never written.
            ok, why = _validate_lesson_quality(
                data.get("lesson_text", ""), data.get("recall_questions", [])
            )
            if not ok:
                _queue_quality_failed(db, oid, why, dry_run)
                _record(summary, oid, len(chunks), sources, final_conf, "queued", verbose)
                summary["queued"] += 1
                if verbose:
                    print(f"    {oid}: quality_check_failed -- {why}")
                continue

            # (f) Write the lesson. lesson_id = sha256(objective_id|generated_at)[:16].
            # On --regenerate, delete the old row here -- only now that a good new
            # lesson is in hand -- then insert (UNIQUE(objective_id) is satisfied).
            # The matching queue rows are deleted in the SAME transaction as the
            # insert (the per-objective commit is in the finally below), so a failed
            # insert rolls the queue delete back too -- the objective stays flagged.
            cleared = 0
            if not dry_run:
                if regenerate:
                    db.execute("DELETE FROM objective_lessons WHERE objective_id = ?", (oid,))
                generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                lesson_id = hashlib.sha256(
                    f"{oid}|{generated_at}".encode("utf-8")
                ).hexdigest()[:16]
                source_chunk_ids = [c["chunk_id"] for c in chunks]
                db.execute(
                    """
                    INSERT INTO objective_lessons
                        (lesson_id, objective_id, subject_id, lesson_text,
                         worked_examples, key_terms, common_mistakes,
                         recall_questions, source_chunk_ids, confidence, generated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lesson_id, oid, subject_id, data.get("lesson_text", ""),
                        json.dumps(data.get("worked_examples", [])),
                        json.dumps(data.get("key_terms", [])),
                        data.get("common_mistakes", ""),
                        json.dumps(data.get("recall_questions", [])),
                        json.dumps(source_chunk_ids),
                        final_conf, generated_at,
                    ),
                )
                # This objective is now covered -- drop any queue rows flagging it.
                cur = db.execute(
                    "DELETE FROM lesson_generation_queue WHERE objective_id = ?", (oid,)
                )
                cleared = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

            summary["cleared"] += cleared
            _record(summary, oid, len(chunks), sources, final_conf, "written", verbose,
                    cleared=cleared)
            summary["written"] += 1
        finally:
            if not dry_run:
                db.commit()

    if verbose:
        _print_totals(summary)
    return summary


def _source_label(chunks: list[dict]) -> str:
    """Comma-separated short source names in notes,papers,schemes order."""
    present = {c["vec_table"] for c in chunks}
    ordered = [SOURCE_NAMES[t] for t in
               (NOTES_TABLE, PAST_PAPERS_TABLE, MARK_SCHEMES_TABLE) if t in present]
    return ",".join(ordered)


def _record(summary: dict, oid: str, chunks_used: int, sources: str,
            confidence, status: str, verbose: bool, cleared: int = 0) -> None:
    summary["rows"].append({
        "objective_id": oid, "chunks_used": chunks_used, "sources": sources,
        "confidence": confidence, "status": status, "cleared": cleared,
    })
    if verbose:
        conf = "  --" if confidence is None else f"{confidence:>4}"
        print(f"  {oid:<16}{chunks_used:>7}  {sources:<18}{conf}{cleared:>8}  {status}")


def _print_totals(summary: dict) -> None:
    print("  " + "-" * 68)
    print(f"  written: {summary['written']}   queued: {summary['queued']}   "
          f"skipped: {summary['skipped']}   errored: {summary['errored']}   "
          f"cleared: {summary['cleared']}")
    if summary["regenerate"]:
        print("  (--regenerate: existing lessons were replaced)")
    print()


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


@backup_first("pre_ingest_lessons")
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-generate one canonical lesson per objective (offline)."
    )
    parser.add_argument("--subject", required=True,
                        help="Subject id, e.g. Principles_of_Business")
    parser.add_argument("--regenerate", action="store_true",
                        help="Replace existing lessons instead of skipping them.")
    parser.add_argument("--confidence-floor", type=int,
                        default=DEFAULT_CONFIDENCE_FLOOR,
                        help="Lessons below this final confidence are queued, not written.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen; change nothing.")
    args = parser.parse_args()

    db = _open_live_db()
    try:
        ingest_lessons_for_subject(
            db, args.subject,
            regenerate=args.regenerate,
            confidence_floor=args.confidence_floor,
            dry_run=args.dry_run,
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
