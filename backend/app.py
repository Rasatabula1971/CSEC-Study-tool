# PHASE: runtime
"""
backend/app.py
==============
Stage 6 FastAPI entry point. Wraps the deterministic controller in JSON endpoints
and serves the single-page chat UI (backend/static/chat.html). The live system is
exactly this app + Ollama (CLAUDE.md).

Lifespan startup:
  1. Verify the SSD is mounted (sys.exit with a clear message if not).
  2. ollama_health() -- log a warning if down, but keep running (the UI still
     loads; chat calls surface the Ollama error).
  3. Open the SSD DB once and stash it on app.state.db.

Run (dev):
    python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
"""

import json
import logging
import os
import sqlite3
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

# backend/ on sys.path so the bare module imports below resolve whether the app
# is launched as `backend.app:app` or imported directly in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ollama_client import ollama_health, ollama_chat, ollama_embed  # noqa: E402
# is_gemini_available is reached through the PHASE: dual router, never imported
# from gemini_client directly: app.py is PHASE: runtime, and runtime modules must
# not import a cloud client (PDR v3.1 VAL-01, enforced by tests/test_pdr_v3_1_compliance).
from llm_router import is_gemini_available  # noqa: E402
from controller import handle_request  # noqa: E402
from schedule import get_due_objectives  # noqa: E402
from study_plan import get_plan_progress  # noqa: E402
from export_progress import export_progress, fetch_progress  # noqa: E402
from notes import classify_notes, save_notes  # noqa: E402
from extract import detect_mime_type, extract_text  # noqa: E402
import uploads  # noqa: E402  -- namespaced to avoid clashing with extract.extract_text

logger = logging.getLogger("csec.app")
# Ensure our startup INFO lines (Ollama / Gemini status) actually surface: the
# logger otherwise has no handler and defaults to WARNING, so INFO is dropped.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

STATIC_DIR = Path(__file__).resolve().parent / "static"
WELCOME_HTML = STATIC_DIR / "welcome.html"
CHAT_HTML = STATIC_DIR / "chat.html"
QUIZ_HTML = STATIC_DIR / "quiz.html"
PLAN_HTML = STATIC_DIR / "study_plan.html"
UPLOAD_HTML = STATIC_DIR / "upload.html"


# Idempotent runtime migration: ensures tables added after a DB was first created
# exist on the live SSD DB without a re-init. CREATE TABLE IF NOT EXISTS is a no-op
# when the table is already present (the canonical schema lives in db/schema.sql).
RUNTIME_MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS practice_questions (
        question_id   TEXT PRIMARY KEY,
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        stem          TEXT NOT NULL,
        created_at    TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_plan (
        plan_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_id  TEXT NOT NULL REFERENCES objectives(objective_id),
        status        TEXT NOT NULL DEFAULT 'unmet',
        met_count     INTEGER NOT NULL DEFAULT 0,
        last_met_at   TEXT,
        created_at    TEXT DEFAULT (datetime('now')),
        UNIQUE(subject_id, objective_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS study_batches (
        batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
        objective_ids   TEXT NOT NULL,
        synthesis_qid   TEXT,
        status          TEXT NOT NULL DEFAULT 'active',
        created_at      TEXT DEFAULT (datetime('now')),
        completed_at    TEXT
    )
    """,
)


def open_db(db_path: str) -> sqlite3.Connection:
    """Open the SSD DB with sqlite-vec loaded and FKs on (same pattern as init_db)."""
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("ERROR: sqlite-vec is not installed. Run: pip install sqlite-vec")
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys = ON")
    db.row_factory = sqlite3.Row
    return db


def _ensure_schema_migrations(db: sqlite3.Connection) -> None:
    """Bootstrap the migration ledger. This is the one migration that cannot be
    version-tracked (it would have to record its own creation before the table it
    records into exists), so it always runs -- CREATE TABLE IF NOT EXISTS makes that
    a no-op once the table is present."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at  TEXT DEFAULT (datetime('now'))
        )
        """
    )
    db.commit()


def _run_migration(db: sqlite3.Connection, version: str, description: str,
                   sql: str) -> bool:
    """Apply a versioned schema migration exactly once.

    Returns True if this call applied (or recorded) the migration, False if it was
    already recorded in schema_migrations. The 'duplicate column name' branch records
    a migration whose ALTER already ran under the old try/except style as applied
    [pre-existing], so we stop re-attempting a column add that can never succeed."""
    row = db.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
    ).fetchone()
    if row:
        return False  # already applied
    try:
        for statement in sql.split(";"):
            s = statement.strip()
            if s:
                db.execute(s)
        db.execute(
            "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
            (version, description),
        )
        db.commit()
        return True
    except Exception as e:  # noqa: BLE001 -- inspected below, re-raised if unexpected
        db.rollback()
        # Some migrations are inherently idempotent (an ALTER ADD COLUMN that already
        # ran in the old try/except style). Record those as applied so we do not keep
        # retrying them on every startup.
        if "duplicate column name" in str(e).lower():
            db.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (version, description + " [pre-existing]"),
            )
            db.commit()
            return True
        raise


def apply_runtime_migrations(db: sqlite3.Connection) -> None:
    """Bring the live DB up to date. Two layers:

      1. Version-tracked SCHEMA migrations (CREATE TABLE / ALTER ADD COLUMN /
         CREATE INDEX) recorded in schema_migrations -- each runs exactly once.
      2. Idempotent DATA-normalisation passes (backfills) that run on EVERY call,
         because they must also catch rows inserted by later ingestion runs, not
         just rows present the first time migrations ran. Their WHERE clauses make
         a re-run a no-op once every row is already normalised.

    Safe to run on every startup and safe to run repeatedly within a process."""
    _ensure_schema_migrations(db)

    # --- Layer 1: version-tracked schema migrations -------------------------
    # m001: the three runtime tables added after the canonical schema.sql (the
    # source of truth) was first written. CREATE TABLE IF NOT EXISTS each, so this
    # is a no-op on a DB that already has them.
    _run_migration(
        db, "m001_runtime_core_tables",
        "practice_questions + study_plan + study_batches",
        ";\n".join(RUNTIME_MIGRATIONS),
    )
    # m002-m006 (Stage 8/9 + PDR v3.1): widen mark_points with provenance columns.
    # SQLite has no ADD COLUMN IF NOT EXISTS; on a DB where the column already exists
    # the ALTER raises 'duplicate column name', which _run_migration records as
    # [pre-existing] so it is never retried.
    _run_migration(db, "m002_stage8_source_type",
                   "mark_points.source_type",
                   "ALTER TABLE mark_points ADD COLUMN source_type TEXT DEFAULT 'past_paper'")
    _run_migration(db, "m003_stage8_source_chunk_id",
                   "mark_points.source_chunk_id",
                   "ALTER TABLE mark_points ADD COLUMN source_chunk_id TEXT")
    _run_migration(db, "m004_stage8_extraction_confidence",
                   "mark_points.extraction_confidence",
                   "ALTER TABLE mark_points ADD COLUMN extraction_confidence INTEGER DEFAULT 100")
    _run_migration(db, "m005_stage9_command_word",
                   "mark_points.command_word",
                   "ALTER TABLE mark_points ADD COLUMN command_word TEXT")
    _run_migration(db, "m006_pdrv31_source_model",
                   "mark_points.source_model",
                   "ALTER TABLE mark_points ADD COLUMN source_model TEXT")
    # m007 (Stage 9 groundwork): queue of objectives whose canonical lesson still
    # needs generating. CREATE TABLE IF NOT EXISTS, so a no-op once present.
    _run_migration(
        db, "m007_stage9_lesson_generation_queue",
        "lesson_generation_queue table",
        """
        CREATE TABLE IF NOT EXISTS lesson_generation_queue (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id  TEXT NOT NULL,
            reason        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
        """,
    )
    # m008 (Stage 11): one pre-generated, source-grounded lesson per objective,
    # composed offline by ingest_lessons.py and served deterministically at runtime
    # (no Ollama call on a teach request). UNIQUE(objective_id) enforces exactly one
    # canonical lesson per objective.
    _run_migration(
        db, "m008_stage11_objective_lessons",
        "objective_lessons table",
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
        """,
    )
    # m009 (Stage 11 fix): UNIQUE index on (objective_id, reason) so requeuing an
    # objective is an idempotent upsert (ingest_lessons._queue_insufficient uses
    # ON CONFLICT on this pair) instead of stacking a fresh row every failed run.
    # The one-off dedup cleanup ran before this index existed, so it now creates
    # cleanly on the live DB.
    _run_migration(
        db, "m009_stage11_lgq_unique_index",
        "idx_lgq_objective_reason unique index",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lgq_objective_reason "
        "ON lesson_generation_queue(objective_id, reason)",
    )
    # m010 (Stage 12): one row per 👍/👎/🤔 tap after a lesson or graded answer. The
    # CHECK constraints make the enum the DB's responsibility; the FKs to
    # objectives/subjects guarantee every flag resolves to a real objective (CLAUDE.md
    # Rule 1). The two indexes back feedback_report.py's group-by query.
    _run_migration(
        db, "m010_stage12_user_feedback",
        "user_feedback table + indexes",
        """
        CREATE TABLE IF NOT EXISTS user_feedback (
            feedback_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     INTEGER,
            objective_id   TEXT NOT NULL REFERENCES objectives(objective_id),
            subject_id     TEXT NOT NULL REFERENCES subjects(subject_id),
            feedback_type  TEXT NOT NULL CHECK (feedback_type IN
                             ('lesson','grading','recall_question')),
            sentiment      TEXT NOT NULL CHECK (sentiment IN
                             ('positive','negative','confused')),
            notes          TEXT,
            context_json   TEXT,
            created_at     TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_feedback_objective
            ON user_feedback(objective_id);
        CREATE INDEX IF NOT EXISTS idx_feedback_sentiment
            ON user_feedback(sentiment, subject_id)
        """,
    )
    # m011 (Stage 13): source_rank ranks the trustworthiness of a mark point's
    # source, 2 (best) .. 4 (generated). Rank 1 is reserved for content_stmt-level
    # content; rank 5 ("generated, unreviewed") is a RUNTIME overlay, never stored.
    # The backfill lives in Layer 2 below so it also reaches newly ingested rows.
    _run_migration(
        db, "m011_stage13_source_rank_column",
        "mark_points.source_rank",
        "ALTER TABLE mark_points ADD COLUMN source_rank INTEGER",
    )
    # m012 (Upload session 1): the staging table for the Upload Material feature.
    # A dropped PDF/DOCX is recorded here and its text extracted for preview; nothing
    # is ingested yet (status stays 'staged' -- 'ingested'/'rejected' arrive in
    # sessions 3-4). extract_status drives the pending->extracting->ready|failed
    # state machine the UI polls on.
    _run_migration(
        db, "m012_upload_session_1",
        "upload_staging table for the upload feature (session 1)",
        """
        CREATE TABLE IF NOT EXISTS upload_staging (
            staging_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
            original_name   TEXT NOT NULL,
            stored_path     TEXT NOT NULL,
            file_type       TEXT NOT NULL CHECK (file_type IN ('pdf','docx')),
            file_size_bytes INTEGER NOT NULL,
            extracted_text  TEXT,
            extract_status  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (extract_status IN
                              ('pending','extracting','ready','failed')),
            extract_error   TEXT,
            status          TEXT NOT NULL DEFAULT 'staged'
                            CHECK (status IN ('staged','ingested','rejected')),
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_upload_staging_subject
            ON upload_staging(subject_id, status)
        """,
    )
    # m013 (Upload session 2): OCR fields + chunked text storage.
    #
    # This must REBUILD upload_staging rather than ALTER it: session 2 adds 'image'
    # as a file_type, but m012's table has CHECK (file_type IN ('pdf','docx')), and
    # SQLite cannot ALTER/DROP a CHECK constraint. So we recreate the table with the
    # widened CHECK and fold in the 5 new columns (ocr_used, ocr_pages_count,
    # ocr_confidence_avg, total_pages, truncated) in the same rebuild, preserving every
    # existing row. The rebuild is safe under foreign_keys=ON because no child table
    # references upload_staging yet (upload_staging_chunks is created AFTER the rename).
    # Version-tracked, so it runs exactly once; the SELECT only names the old columns,
    # so it tolerates a DB where a partial earlier run already added some new columns.
    _run_migration(
        db, "m013_upload_session_2",
        "OCR fields and chunked text storage for session 2",
        """
        CREATE TABLE upload_staging_new (
            staging_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id      TEXT NOT NULL REFERENCES subjects(subject_id),
            original_name   TEXT NOT NULL,
            stored_path     TEXT NOT NULL,
            file_type       TEXT NOT NULL CHECK (file_type IN ('pdf','docx','image')),
            file_size_bytes INTEGER NOT NULL,
            extracted_text  TEXT,
            extract_status  TEXT NOT NULL DEFAULT 'pending'
                            CHECK (extract_status IN
                              ('pending','extracting','ready','failed')),
            extract_error   TEXT,
            status          TEXT NOT NULL DEFAULT 'staged'
                            CHECK (status IN ('staged','ingested','rejected')),
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT,
            ocr_used            INTEGER DEFAULT 0,
            ocr_pages_count     INTEGER DEFAULT 0,
            ocr_confidence_avg  INTEGER,
            total_pages         INTEGER,
            truncated           INTEGER DEFAULT 0
        );
        INSERT INTO upload_staging_new
            (staging_id, subject_id, original_name, stored_path, file_type,
             file_size_bytes, extracted_text, extract_status, extract_error,
             status, created_at, updated_at)
        SELECT staging_id, subject_id, original_name, stored_path, file_type,
               file_size_bytes, extracted_text, extract_status, extract_error,
               status, created_at, updated_at
        FROM upload_staging;
        DROP TABLE upload_staging;
        ALTER TABLE upload_staging_new RENAME TO upload_staging;
        CREATE INDEX IF NOT EXISTS idx_upload_staging_subject
            ON upload_staging(subject_id, status);
        CREATE TABLE IF NOT EXISTS upload_staging_chunks (
            chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            staging_id      INTEGER NOT NULL REFERENCES upload_staging(staging_id) ON DELETE CASCADE,
            chunk_index     INTEGER NOT NULL,
            chunk_text      TEXT NOT NULL,
            page_start      INTEGER,
            page_end        INTEGER,
            ocr_used        INTEGER DEFAULT 0,
            UNIQUE (staging_id, chunk_index)
        );
        CREATE INDEX IF NOT EXISTS idx_staging_chunks_staging
            ON upload_staging_chunks(staging_id)
        """,
    )
    # m014 (Upload session 2 follow-up): flag files whose OCR ran at a reduced render
    # DPI because a page was too large for Pillow's decompression-bomb guard at OCR_DPI.
    # session 3 surfaces this as a "reduced resolution" review badge. Single ALTER --
    # _run_migration records it [pre-existing] if the column already exists.
    _run_migration(
        db, "m014_upload_ocr_dpi_reduced",
        "upload_staging.ocr_dpi_reduced",
        "ALTER TABLE upload_staging ADD COLUMN ocr_dpi_reduced INTEGER DEFAULT 0",
    )

    # --- Layer 2: idempotent data-normalisation passes (run on EVERY call) ---
    # NOT version-tracked: a later ingestion run inserts fresh rows that still need
    # normalising, so each pass runs every startup. The WHERE clauses make a re-run a
    # no-op once every row is already normalised. (Tests rely on this: they insert
    # mark_points then call apply_runtime_migrations again to backfill the new rows.)

    # Stage 10: backfill command_word where the objective has exactly one command
    # word (an unambiguous gate). Rows under multi-word objectives stay NULL so the
    # examiner falls back to the question-level word. json_extract/json_array_length
    # need JSON1 (bundled in modern SQLite); wrapped so an old build degrades quietly.
    try:
        db.execute(
            """
            UPDATE mark_points
            SET command_word = (
                SELECT json_extract(o.command_words, '$[0]')
                FROM   objectives o
                WHERE  o.objective_id = mark_points.objective_id
            )
            WHERE command_word IS NULL
              AND (
                SELECT json_array_length(o.command_words)
                FROM   objectives o
                WHERE  o.objective_id = mark_points.objective_id
              ) = 1
            """
        )
    except sqlite3.OperationalError:
        pass  # JSON1 unavailable or malformed command_words -- leave command_word NULL

    # Stage 13: backfill source_rank from source_type + the document content_type.
    # Only NULL rows are touched, so a re-run is a no-op. Wrapped so a very old DB
    # without source_type degrades quietly (leaves source_rank NULL).
    try:
        db.execute(
            """
            UPDATE mark_points
            SET source_rank = CASE
                WHEN source_type = 'past_paper' AND (
                     SELECT d.content_type FROM documents d
                     WHERE d.doc_id = mark_points.doc_id
                ) = 'specimen'                                    THEN 2
                WHEN source_type = 'past_paper'                   THEN 3
                WHEN source_type IN ('recovered_extraction',
                                     'syllabus_derived')          THEN 4
                ELSE NULL
            END
            WHERE source_rank IS NULL
            """
        )
    except sqlite3.OperationalError:
        pass  # source_type column absent on a very old DB -- leave source_rank NULL

    # Normalise question_id to the -stem convention used by ingest_solutions.py. Old
    # PDF-ingester rows stored question_id without the suffix; this makes the
    # grade-picker join work for all rows. Idempotent via the NOT LIKE guard.
    try:
        db.execute(
            """
            UPDATE mark_points
            SET    question_id = question_id || '-stem'
            WHERE  question_id NOT LIKE '%-stem'
            """
        )
        db.commit()
    except sqlite3.OperationalError as e:
        # Database may be locked by a concurrent ingest. The backfill
        # is idempotent -- it will run on a future startup when the
        # lock is released. Log and continue rather than crashing app
        # startup.
        logger.warning(
            "question_id backfill skipped: %s (will retry on next startup)",
            e,
        )

    # Upload session 1: make sure the SSD staging tree exists for every locked
    # subject. Best-effort -- a missing SSD just logs a warning here and surfaces a
    # clearer error at upload time.
    ensure_staging_dirs(db)


def ensure_staging_dirs(db: sqlite3.Connection) -> None:
    """Create {SSD_ROOT}/06_UPLOAD_STAGING and a subdir per locked subject.

    Called from apply_runtime_migrations so the tree exists before the first
    upload. If the SSD is not mounted (or SSD_ROOT is unset), log a warning and
    skip -- the upload endpoint fails with a clearer message at write time.
    """
    ssd_root = os.getenv("SSD_ROOT")
    if not ssd_root:
        logger.warning("SSD_ROOT not set -- skipping upload-staging directory setup.")
        return
    staging_root = Path(ssd_root) / "06_UPLOAD_STAGING"
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        rows = db.execute(
            "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
        ).fetchall()
        for r in rows:
            (staging_root / r["subject_id"]).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create upload-staging dirs under %s (%s) -- uploads will "
            "fail until the SSD is mounted.", staging_root, exc,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ssd_root = os.getenv("SSD_ROOT")
    if ssd_root and not os.path.exists(ssd_root):
        sys.exit(f"ERROR: SSD not mounted at {ssd_root}. Plug in the drive and restart.")

    if ollama_health():
        # Pre-warm: one tiny chat call loads the 3B chat model into RAM now (held by
        # ollama_chat's keep_alive=30m), so the first Submit of the session doesn't
        # pay the cold model-load tax. Non-fatal -- a failure just warns.
        try:
            ollama_chat([{"role": "user", "content": "ready"}],
                        system="Respond with one word: ready.")
        except Exception as exc:
            logger.warning("Ollama pre-warm failed (%s) -- first response may be slow.", exc)
    else:
        logger.warning("Ollama is not reachable at %s -- study mode will surface the "
                        "error. Starting the app anyway.", os.getenv("OLLAMA_BASE"))

    # Optional Cloud Mode (CLAUDE.md): grading uses Gemini ONLY when CLOUD_MODE=1.
    # In offline mode (CLOUD_MODE=0, the default) we make NO Gemini call at all --
    # not even a reachability ping. The mode is explicit; there is no silent
    # fallback in either direction (see llm_router.chat_for_grading).
    cloud_mode = os.getenv("CLOUD_MODE", "0") == "1"
    if cloud_mode:
        reachable = is_gemini_available()
        logger.info("Cloud mode enabled -- Gemini reachable: %s", reachable)
        if not reachable:
            logger.warning("CLOUD_MODE=1 but Gemini unreachable. Grading requests will "
                           "fail until Gemini is reachable or CLOUD_MODE is set to 0.")
    else:
        logger.info("Offline mode -- all inference via Ollama.")

    db_path = os.getenv("DB_PATH")
    if not db_path or not os.path.exists(db_path):
        sys.exit(f"ERROR: database not found at {db_path}. Run init_db.py first.")
    app.state.db = open_db(db_path)
    apply_runtime_migrations(app.state.db)
    try:
        yield
    finally:
        app.state.db.close()


app = FastAPI(title="CSEC AI Study Partner", lifespan=lifespan)

# Serve the static assets (the chat UI lives here).
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    """A chat turn. `message` is the student's text; `route` selects the workflow.

    message and subject_id are required and non-empty (missing OR empty -> 422).
    Optional fields (question_id, objective_id, paper, year, question_num,
    content_type) pass through to the controller when present -- e.g. a grade
    turn supplies question_id.
    """
    message: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    route: str
    question_id: str | None = None
    objective_id: str | None = None
    paper: str | None = None
    year: int | None = None
    question_num: str | None = None
    content_type: str | None = None


class StartBatchRequest(BaseModel):
    """Open a new study-plan batch for a subject."""
    subject_id: str = Field(min_length=1)


class BatchQuestionRequest(BaseModel):
    """Ask for the question at one step of a batch. step = "1".."N" | "synthesis".

    lesson_context: the lesson text the student just read (already stripped of its
    own trailing question). When present, the generator is constrained to test what
    that lesson actually taught, so the question card stays aligned with the lesson.
    """
    batch_id: int
    step: str = Field(min_length=1)
    lesson_context: str | None = None


class GradeBatchRequest(BaseModel):
    """Grade one batch answer (per-objective or synthesis).

    Per-objective steps (single-call architecture) send objective_id + question_text:
    the question was extracted from the lesson client-side, so there is no stored
    question to resolve. Synthesis (and the no-lesson fallback) still send question_id.
    """
    batch_id: int
    question_id: str | None = None
    objective_id: str | None = None
    question_text: str | None = None
    answer: str = Field(min_length=1)


class MissedPoint(BaseModel):
    """One mark point the student missed, as returned in a grade result."""
    mark_point_id: str | None = None
    expected: str | None = None
    evidence: str | None = None


class ExplainMissedRequest(BaseModel):
    """Ask for a plain-language explanation of the points missed on one objective."""
    subject_id: str = Field(min_length=1)
    objective_id: str = Field(min_length=1)
    missed_points: list[MissedPoint] = []


class ClassifyNotesRequest(BaseModel):
    """Classify pasted/uploaded note text: which subject + objectives it belongs to."""
    text: str = Field(min_length=1, max_length=50000)
    available_subjects: list[str] = []


class FeedbackRequest(BaseModel):
    """One 👍/👎/🤔 tap after a lesson or graded answer (Stage 12).

    feedback_type and sentiment are Literal enums, so an unknown value is rejected
    by Pydantic with a 422 before the endpoint body runs. objective_id/subject_id
    are validated against the FK at INSERT time, not pre-checked here.
    """
    objective_id: str
    subject_id: str
    feedback_type: Literal['lesson', 'grading', 'recall_question']
    sentiment: Literal['positive', 'negative', 'confused']
    notes: Optional[str] = None
    context_json: Optional[str] = None
    session_id: Optional[int] = None


# ---------------------------------------------------------------------------
# UI compatibility shim
# ---------------------------------------------------------------------------
def _shape_for_ui(result: dict) -> dict:
    """Add aliases the chat UI expects, without touching the controller output.

    - plan: the UI reads `objectives`; the controller returns `tasks`.
    - grade: the UI reads top-level `leitner_box`/`next_review`; the controller
      nests them under `weakness`.
    These are additive (the original keys remain) and presentation-only.
    """
    if not isinstance(result, dict):
        return result
    if "tasks" in result and "objectives" not in result:
        result["objectives"] = result["tasks"]
    weakness = result.get("weakness")
    if isinstance(weakness, dict):
        result.setdefault("leitner_box", weakness.get("leitner_box"))
        result.setdefault("next_review", weakness.get("next_review"))
    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/quiz")
def quiz_page() -> FileResponse:
    """Serve the dedicated full-page quiz experience (backend/static/quiz.html)."""
    return FileResponse(QUIZ_HTML)


@app.get("/plan")
def plan_page() -> FileResponse:
    """Serve the standalone Study Plan page (backend/static/study_plan.html)."""
    return FileResponse(PLAN_HTML)


@app.get("/upload")
def upload_page() -> FileResponse:
    """Serve the Upload Material page (backend/static/upload.html)."""
    return FileResponse(UPLOAD_HTML)


@app.get("/")
def index() -> FileResponse:
    """Serves chat.html, the live UI. The Stage 13 panel shell was reverted on
    2026-06-17 (preserved at chat_panel_shell.html.bak); chat.html is the v1 chat
    UI again. The Welcome page remains on disk at /welcome."""
    return FileResponse(CHAT_HTML)


@app.get("/chat")
def chat_page() -> FileResponse:
    """The panel shell, also reachable at /chat (kept for existing bookmarks)."""
    return FileResponse(CHAT_HTML)


@app.get("/welcome")
def welcome_page() -> FileResponse:
    """The previous Welcome front door (greeting, add-notes, navigation), preserved."""
    return FileResponse(WELCOME_HTML)


@app.get("/health")
def health(request: Request) -> dict:
    db_ok = getattr(request.app.state, "db", None) is not None
    return {"status": "ok", "ollama": ollama_health(), "db": db_ok}


@app.get("/api/status")
def status(request: Request) -> dict:
    """Honest reflection of what grading WILL actually do (CLAUDE.md Cloud Mode).

    grading_engine matches llm_router.chat_for_grading exactly: it is driven by
    CLOUD_MODE, not by live health. When CLOUD_MODE=0 we make NO Gemini call at
    all -- gemini_available is reported False without a ping. When CLOUD_MODE=1 we
    check is_gemini_available() (the same predicate the router gates on); a True
    grading_engine with gemini_available=False honestly says "configured for cloud,
    but currently unreachable -- grading will error until it recovers".
    """
    cloud_mode = os.getenv("CLOUD_MODE", "0") == "1"
    ollama_up = ollama_health()
    db_ok = getattr(request.app.state, "db", None) is not None
    # No ping in offline mode: gemini_available stays False and is_gemini_available
    # is never called when CLOUD_MODE=0.
    gemini_available = is_gemini_available() if cloud_mode else False
    grading_engine = "gemini" if cloud_mode else "ollama"
    return {
        "status": "ok",
        "ollama": ollama_up,
        "db": db_ok,
        "cloud_mode": cloud_mode,
        "gemini_available": gemini_available,
        "grading_engine": grading_engine,
    }


@app.get("/api/subjects")
def subjects(request: Request) -> list[dict]:
    """Locked subjects only -- the UI must not offer a subject that is out of scope."""
    rows = request.app.state.db.execute(
        "SELECT subject_id, display_name FROM subjects WHERE syllabus_locked = 1 "
        "ORDER BY display_name"
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/due/{subject_id}")
def due(subject_id: str, request: Request) -> list[dict]:
    return get_due_objectives(request.app.state.db, subject_id)


@app.get("/api/questions/{subject_id}")
def questions(subject_id: str, request: Request) -> list[dict]:
    """Gradeable questions for a subject -- those that have mark points.

    Powers the grade-mode question picker: each entry carries the question_id
    grade.fetch_mark_points keys on, the question prose (stem chunk), and a
    human label. Marks = number of mark points for that question.
    """
    rows = request.app.state.db.execute(
        """
        SELECT mp.question_id            AS question_id,
               mp.objective_id           AS objective_id,
               c.chunk_text              AS question_text,
               c.question_num            AS question_num,
               d.paper                   AS paper,
               d.year                    AS year,
               COUNT(mp.mark_point_id)   AS marks
        FROM   mark_points mp
        JOIN   documents d ON d.doc_id = mp.doc_id
        LEFT   JOIN chunks c ON c.chunk_id = mp.question_id
        WHERE  d.subject_id = ?
        GROUP  BY mp.question_id
        ORDER  BY d.year DESC, d.paper, mp.question_id
        """,
        (subject_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # paper already carries the year ("Paper 2 - January 2026"), so don't
        # prefix it again -- keep the label free of a duplicated year.
        d["label"] = f"{d['paper']} · Q{d['question_num'] or ''}".strip()
        out.append(d)
    return out


@app.get("/api/questions")
def questions_by_filter(
    request: Request,
    subject_id: str,
    paper: str | None = None,
    year: int | None = None,
) -> list[dict]:
    """Past-paper questions for the dedicated quiz page (/quiz).

    Filters chunks by subject, content_type in (past_paper, mark_scheme), a
    present question_num, and a solution-derived chunk_id ('...-stem'), joined to
    documents for paper/year. The '-stem' filter hides papers whose chunks came
    from ingest.py's MCQ chunker (garbled for older Paper 2 PDFs) until that
    chunker is rewritten -- only ingest_solutions.py questions appear. `paper`
    and `year` are optional query params that narrow the list further. Each row
    carries the question stem (first 400 chars) and a marks_total = number of
    mark points keyed on that question. Returns [] when nothing matches -- never
    404.
    """
    sql = [
        "SELECT c.chunk_id      AS question_id,",
        "       c.question_num  AS question_num,",
        "       d.paper         AS paper,",
        "       d.year          AS year,",
        "       SUBSTR(c.chunk_text, 1, 400) AS stem,",
        "       (SELECT COUNT(*) FROM mark_points mp",
        "          WHERE mp.question_id = c.chunk_id) AS marks_total",
        "FROM   chunks c",
        "JOIN   documents d ON d.doc_id = c.doc_id",
        "WHERE  c.subject_id = ?",
        "  AND  d.content_type IN ('past_paper', 'mark_scheme')",
        "  AND  c.question_num IS NOT NULL",
        # Only show solution-derived questions (chunk_id like 'POB-...-stem',
        # the ingest_solutions.py convention). ingest.py's MCQ chunker still
        # produces garbled chunks for older Paper 2 PDFs -- missing stems,
        # options split across chunks, OCR artifacts ("U nski lied") -- so its
        # auto-generated chunk_ids are excluded until the chunker is rewritten.
        "  AND  c.chunk_id LIKE '%-stem'",
    ]
    params: list = [subject_id]
    if paper:
        sql.append("  AND  d.paper = ?")
        params.append(paper)
    if year is not None:
        sql.append("  AND  d.year = ?")
        params.append(year)
    sql.append("ORDER BY d.year DESC, c.question_num ASC")
    rows = request.app.state.db.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/filters")
def filters(request: Request, subject_id: str) -> dict:
    """Distinct paper/year values that actually have questions for a subject.

    Powers the quiz-page Paper and Year dropdowns: only values that the
    /api/questions query can return (past_paper OR mark_scheme chunks with a
    question_num AND a solution-derived '-stem' chunk_id) appear, so a selected
    paper/year always yields well-formed questions. `papers` is sorted
    alphabetically, `years` descending. Returns empty lists when the subject has
    no questions -- never 404.
    """
    db = request.app.state.db
    papers = db.execute(
        """
        SELECT DISTINCT d.paper AS paper
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme')
          AND  c.question_num IS NOT NULL
          -- Only papers with solution-derived ('-stem') chunks; ingest.py's
          -- MCQ chunker yields garbled chunks for older Paper 2 PDFs, so its
          -- papers are hidden until that chunker is rewritten. Mirrors the
          -- /api/questions filter so a selected paper always yields questions.
          AND  c.chunk_id LIKE '%-stem'
          AND  d.paper IS NOT NULL
        ORDER  BY d.paper ASC
        """,
        (subject_id,),
    ).fetchall()
    years = db.execute(
        """
        SELECT DISTINCT d.year AS year
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  d.content_type IN ('past_paper', 'mark_scheme')
          AND  c.question_num IS NOT NULL
          -- Only years with solution-derived ('-stem') chunks; see papers query.
          AND  c.chunk_id LIKE '%-stem'
          AND  d.year IS NOT NULL
        ORDER  BY d.year DESC
        """,
        (subject_id,),
    ).fetchall()
    return {
        "papers": [r["paper"] for r in papers],
        "years": [r["year"] for r in years],
    }


@app.get("/api/sections")
def sections(request: Request, subject_id: str) -> list[dict]:
    """Syllabus sections (with their objectives) for the quiz-page Practice mode.

    Powers the SECTION and OBJECTIVE dropdowns: each section carries its objectives
    nested, so choosing a section populates the objective list with no extra round
    trip. Returns [] for an unknown subject -- never 404.
    """
    db = request.app.state.db
    secs = db.execute(
        "SELECT section_id, title, section_num FROM syllabus_sections "
        "WHERE subject_id = ? ORDER BY section_num",
        (subject_id,),
    ).fetchall()
    out = []
    for s in secs:
        objs = db.execute(
            "SELECT objective_id, objective_num, content_stmt FROM objectives "
            "WHERE section_id = ? ORDER BY objective_num",
            (s["section_id"],),
        ).fetchall()
        d = dict(s)
        d["objectives"] = [dict(o) for o in objs]
        out.append(d)
    return out


@app.get("/api/objectives/{subject_id}")
def objectives(subject_id: str, request: Request) -> list[dict]:
    """All objectives for a subject, in syllabus order (section_num, objective_num).

    Powers the Welcome-page manual-pick fallback (subject + objective dropdowns)
    when note classification can't determine the subject. Returns [] for an unknown
    subject -- never 404.
    """
    rows = request.app.state.db.execute(
        """
        SELECT o.objective_id, o.content_stmt, o.objective_num,
               s.title       AS section_title,
               s.section_num AS section_num
        FROM   objectives o
        JOIN   syllabus_sections s ON s.section_id = o.section_id
        WHERE  o.subject_id = ?
        ORDER  BY s.section_num, o.objective_num
        """,
        (subject_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Stage 13 panel-shell endpoints (syllabus tree / progress heatmap / papers)
# ---------------------------------------------------------------------------
def _decode_command_words(raw) -> list[str]:
    """objectives.command_words is a JSON array string -> Python list ([] if absent)."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [str(c) for c in val] if isinstance(val, list) else [str(val)]
    except (json.JSONDecodeError, TypeError):
        return [str(raw)]


@app.get("/api/syllabus/{subject_id}")
def syllabus(subject_id: str, request: Request) -> dict:
    """Section + objective tree for the Learn and Library panels.

    Each objective carries has_lesson (a row in objective_lessons), mark_point_count
    (COUNT on mark_points), and best_source_rank (MIN(source_rank)). command_words
    is JSON-decoded to a list. Returns empty sections for an unknown subject.
    """
    db = request.app.state.db
    subj = db.execute(
        "SELECT subject_id, display_name FROM subjects WHERE subject_id = ?",
        (subject_id,),
    ).fetchone()
    display_name = subj["display_name"] if subj else subject_id

    secs = db.execute(
        "SELECT section_id, section_num, title FROM syllabus_sections "
        "WHERE subject_id = ? ORDER BY CAST(section_num AS INTEGER), section_num",
        (subject_id,),
    ).fetchall()

    sections = []
    for s in secs:
        objs = db.execute(
            """
            SELECT o.objective_id, o.objective_num, o.content_stmt,
                   o.command_words, o.skill_type,
                   (SELECT 1 FROM objective_lessons l
                     WHERE l.objective_id = o.objective_id LIMIT 1)        AS has_lesson,
                   (SELECT COUNT(*) FROM mark_points mp
                     WHERE mp.objective_id = o.objective_id)               AS mark_point_count,
                   (SELECT MIN(mp.source_rank) FROM mark_points mp
                     WHERE mp.objective_id = o.objective_id)               AS best_source_rank
            FROM   objectives o
            WHERE  o.section_id = ?
            ORDER  BY o.objective_num
            """,
            (s["section_id"],),
        ).fetchall()
        sections.append({
            "section_id": s["section_id"],
            "section_num": s["section_num"],
            "title": s["title"],
            "objectives": [{
                "objective_id": o["objective_id"],
                "objective_num": o["objective_num"],
                "content_stmt": o["content_stmt"],
                "command_words": _decode_command_words(o["command_words"]),
                "skill_type": o["skill_type"],
                "has_lesson": bool(o["has_lesson"]),
                "mark_point_count": o["mark_point_count"] or 0,
                "best_source_rank": o["best_source_rank"],
            } for o in objs],
        })
    return {"subject_id": subject_id, "display_name": display_name, "sections": sections}


@app.get("/api/progress/{subject_id}")
def progress(subject_id: str, request: Request) -> dict:
    """Per-objective progress for the Progress heatmap -- ALL objectives, not just
    those with a weakness_log row. NULLs where no data exists yet.

    latest_score_pct / last_studied come from the most recent study_sessions row;
    leitner_box / next_review from weakness_log; feedback counts from user_feedback.
    """
    db = request.app.state.db
    rows = db.execute(
        """
        SELECT o.objective_id,
               o.objective_num,
               SUBSTR(o.content_stmt, 1, 80)                       AS content_stmt,
               w.leitner_box                                        AS leitner_box,
               w.next_review                                        AS next_review,
               (SELECT ss.score_pct FROM study_sessions ss
                 WHERE ss.objective_id = o.objective_id
                 ORDER BY ss.created_at DESC LIMIT 1)               AS latest_score_pct,
               (SELECT ss.created_at FROM study_sessions ss
                 WHERE ss.objective_id = o.objective_id
                 ORDER BY ss.created_at DESC LIMIT 1)               AS last_studied,
               (SELECT COUNT(*) FROM user_feedback f
                 WHERE f.objective_id = o.objective_id AND f.sentiment = 'negative') AS feedback_negative,
               (SELECT COUNT(*) FROM user_feedback f
                 WHERE f.objective_id = o.objective_id AND f.sentiment = 'confused') AS feedback_confused
        FROM   objectives o
        JOIN   syllabus_sections s ON s.section_id = o.section_id
        LEFT   JOIN weakness_log w ON w.objective_id = o.objective_id
                                  AND w.subject_id   = o.subject_id
        WHERE  o.subject_id = ?
        ORDER  BY CAST(s.section_num AS INTEGER), o.objective_num
        """,
        (subject_id,),
    ).fetchall()
    return {"objectives": [{
        "objective_id": r["objective_id"],
        "objective_num": r["objective_num"],
        "content_stmt": r["content_stmt"],
        "leitner_box": r["leitner_box"],
        "latest_score_pct": r["latest_score_pct"],
        "last_studied": r["last_studied"],
        "next_review": r["next_review"],
        "feedback_negative": r["feedback_negative"] or 0,
        "feedback_confused": r["feedback_confused"] or 0,
    } for r in rows]}


@app.get("/api/past-papers/{subject_id}")
def past_papers(subject_id: str, request: Request) -> dict:
    """Gradeable past papers for the Practice and Exam panels.

    A "paper" is one document carrying solution-derived ('-stem') question chunks --
    the same gradeable set the quiz page uses. doc_id is included (the Exam/Practice
    flow loads questions by doc_id + question_num). objectives_covered is the
    distinct objective_ids those questions resolve to. Sorted by year desc, paper.
    """
    db = request.app.state.db
    docs = db.execute(
        """
        SELECT d.doc_id, d.year, d.paper,
               COUNT(DISTINCT c.question_num) AS question_count
        FROM   chunks c
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
          AND  c.question_num IS NOT NULL
          AND  c.chunk_id LIKE '%-stem'
        GROUP  BY d.doc_id
        HAVING question_count > 0
        ORDER  BY d.year DESC, d.paper
        """,
        (subject_id,),
    ).fetchall()
    papers = []
    for d in docs:
        objs = db.execute(
            "SELECT DISTINCT objective_id FROM chunks "
            "WHERE doc_id = ? AND question_num IS NOT NULL AND chunk_id LIKE '%-stem' "
            "ORDER BY objective_id",
            (d["doc_id"],),
        ).fetchall()
        papers.append({
            "doc_id": d["doc_id"],
            "year": d["year"],
            "paper": d["paper"],
            "question_count": d["question_count"],
            "objectives_covered": [o["objective_id"] for o in objs],
        })
    return {"papers": papers}


@app.get("/api/practice-question/{doc_id}/{question_num}")
def practice_question(doc_id: str, question_num: str, request: Request) -> dict:
    """One gradeable question (its '-stem' chunk) for the Practice and Exam panels.

    question_id (the chunk_id grade keys on) is returned alongside the prose,
    bound objective, marks_total (mark-point count), and the objective's command
    words. 404 when no matching '-stem' chunk exists.
    """
    db = request.app.state.db
    row = db.execute(
        """
        SELECT c.chunk_id AS question_id, c.question_num, c.chunk_text, c.objective_id,
               (SELECT COUNT(*) FROM mark_points mp WHERE mp.question_id = c.chunk_id) AS marks_total,
               o.command_words
        FROM   chunks c
        JOIN   objectives o ON o.objective_id = c.objective_id
        WHERE  c.doc_id = ? AND c.question_num = ? AND c.chunk_id LIKE '%-stem'
        LIMIT  1
        """,
        (doc_id, question_num),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="question not found")
    return {
        "question_id": row["question_id"],
        "question_num": row["question_num"],
        "question_text": row["chunk_text"],
        "objective_id": row["objective_id"],
        "marks_total": row["marks_total"] or 0,
        "command_words": _decode_command_words(row["command_words"]),
    }


@app.post("/api/chat")
def chat(body: ChatRequest, request: Request) -> dict:
    """Map a chat turn onto the controller's request shape and return its result."""
    req = body.model_dump(exclude_none=True)
    # The controller reads `query` (teach) and `student_answer` (grade); both come
    # from the single message box in the UI.
    req["query"] = body.message
    req["student_answer"] = body.message
    result = handle_request(request.app.state.db, req)
    return _shape_for_ui(result)


@app.post("/api/feedback")
def feedback(body: FeedbackRequest, request: Request, response: Response) -> dict:
    """Log one 👍/👎/🤔 tap after a lesson or graded answer (Stage 12).

    Pydantic has already rejected unknown enum values (422) before this runs. The
    only existence check is the FK on objective_id/subject_id -- an unknown id
    raises sqlite3.IntegrityError, which we map to 400. PRAGMA foreign_keys is ON
    for the app DB (open_db sets it), so the FK is genuinely enforced. Any other
    DB error is a 500. The body shape is {ok, feedback_id} / {ok, error} either way.
    """
    db = request.app.state.db
    try:
        cur = db.execute(
            "INSERT INTO user_feedback "
            "(session_id, objective_id, subject_id, feedback_type, sentiment, "
            " notes, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.session_id, body.objective_id, body.subject_id,
             body.feedback_type, body.sentiment, body.notes, body.context_json),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # FK violation: objective_id or subject_id does not exist. (IntegrityError
        # is a subclass of sqlite3.Error, so this branch must precede the 500 one.)
        response.status_code = 400
        return {"ok": False, "error": "unknown objective_id or subject_id"}
    except sqlite3.Error as exc:
        response.status_code = 500
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "feedback_id": cur.lastrowid}


# ---------------------------------------------------------------------------
# Study Plan endpoints
# ---------------------------------------------------------------------------
@app.post("/api/plan/start_batch")
def plan_start_batch(body: StartBatchRequest, request: Request) -> dict:
    """Seed the plan (idempotent) and open the next batch of objectives."""
    req = {"route": "start_batch", "subject_id": body.subject_id}
    return handle_request(request.app.state.db, req)


@app.post("/api/plan/batch_question")
def plan_batch_question(body: BatchQuestionRequest, request: Request) -> dict:
    """Generate the question for one step of a batch (per-objective or synthesis)."""
    req = {"route": "batch_question", "batch_id": body.batch_id, "step": body.step}
    if body.lesson_context:
        req["lesson_context"] = body.lesson_context
    return handle_request(request.app.state.db, req)


@app.post("/api/plan/grade_batch")
def plan_grade_batch(body: GradeBatchRequest, request: Request) -> dict:
    """Grade one batch answer and return the result plus updated plan progress."""
    req = {
        "route": "grade_batch_question",
        "batch_id": body.batch_id,
        "answer": body.answer,
    }
    # Forward only what was supplied: question_id for synthesis/fallback, or
    # objective_id + question_text for the extracted per-objective question.
    if body.question_id:
        req["question_id"] = body.question_id
    if body.objective_id:
        req["objective_id"] = body.objective_id
    if body.question_text is not None:
        req["question_text"] = body.question_text
    result = handle_request(request.app.state.db, req)
    return _shape_for_ui(result)


@app.post("/api/plan/explain_missed")
def plan_explain_missed(body: ExplainMissedRequest, request: Request) -> dict:
    """Explain the concepts a student missed on one per-objective step.

    Returns {"feedback": "..."}; an empty missed_points list returns
    {"feedback": ""} without an LLM call (the controller short-circuits).
    """
    req = {
        "route": "explain_missed",
        "subject_id": body.subject_id,
        "objective_id": body.objective_id,
        "missed_points": [mp.model_dump() for mp in body.missed_points],
    }
    return handle_request(request.app.state.db, req)


@app.get("/api/plan/progress/{subject_id}")
def plan_progress(subject_id: str, request: Request) -> dict:
    """Mastery counts for a subject's study plan (deterministic aggregation)."""
    return get_plan_progress(request.app.state.db, subject_id)


@app.get("/api/export/progress/{subject_id}")
def export_progress_report(subject_id: str, request: Request) -> FileResponse:
    """Generate a colour-coded study-progress workbook and return it as a download.

    Same logic as backend/export_progress.py (the CLI). Lets the UI add a "Download
    Progress Report" button. Returns the .xlsx as an attachment; 404 if the subject
    has no objectives.
    """
    db = request.app.state.db
    reports_root = os.getenv("REPORTS_ROOT")
    if not reports_root:
        raise HTTPException(status_code=500, detail="REPORTS_ROOT not configured")
    if not fetch_progress(db, subject_id):
        raise HTTPException(status_code=404, detail=f"No objectives for subject '{subject_id}'")
    path = export_progress(db, subject_id, reports_root, date.today().isoformat())
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Add Study Notes (Welcome page)
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB (52428800 bytes)
MAX_NOTE_CHARS = 50_000


def _extract_upload_text(data: bytes, filename: str) -> str:
    """Extract plain text from uploaded file bytes (PDF/DOCX/TXT/JPG/PNG).

    Writes the bytes to a temp file (extract.extract_text and its libraries take a
    path), dispatches on the filename's mime type, then cleans up. Raises
    ValueError -- which the caller maps to a 400 -- for unsupported types or when
    the Tesseract OCR toolchain is missing for an image.
    """
    mime = detect_mime_type(filename)  # ValueError -> caller returns 400
    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return extract_text(str(tmp_path), mime)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/notes/classify")
def notes_classify(body: ClassifyNotesRequest, request: Request) -> dict:
    """Decide which subject + objectives a note excerpt belongs to.

    The LLM picks the subject (schema-constrained); a deterministic cosine pass
    ranks the subject's objectives. Returns subject_id (may be null), confidence,
    reasoning, and up to three suggested_objectives. If no subject is offered,
    falls back to the locked-subject list from the DB.
    """
    available = body.available_subjects
    if not available:
        rows = request.app.state.db.execute(
            "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
        ).fetchall()
        available = [r["subject_id"] for r in rows]
    return classify_notes(
        request.app.state.db, body.text, available,
        chat_fn=ollama_chat, embed_fn=ollama_embed,
    )


@app.post("/api/notes/classify_file")
async def notes_classify_file(
    request: Request,
    file: UploadFile = File(...),
) -> dict:
    """Extract text from an uploaded file, then classify it like /classify.

    Accepts a PDF, DOCX, TXT, JPG, or PNG. The full text is extracted server-side
    (images via Tesseract OCR); the first 2000 chars feed the same LLM-subject +
    deterministic-objective classifier as /api/notes/classify. Returns that result
    plus `extracted_text_length` so the UI can confirm how much text it read. The
    caller saves by re-sending the file to /api/notes/upload.
    """
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")
    try:
        full_text = _extract_upload_text(data, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    full_text = (full_text or "").strip()
    if not full_text:
        raise HTTPException(
            status_code=422,
            detail="No text could be extracted from the file.",
        )

    rows = request.app.state.db.execute(
        "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
    ).fetchall()
    available = [r["subject_id"] for r in rows]

    result = classify_notes(
        request.app.state.db, full_text[:2000], available,
        chat_fn=ollama_chat, embed_fn=ollama_embed,
    )
    result["extracted_text_length"] = len(full_text)
    return result


@app.post("/api/notes/upload")
async def notes_upload(request: Request) -> dict:
    """Chunk, embed, and index confirmed notes under a subject + objective.

    Handles two request shapes (the paste path sends JSON, the upload path sends
    multipart/form-data):
      * JSON      -> {subject_id, objective_id, text}
      * multipart -> subject_id, objective_id, and either a `text` field or a
        `file` field (PDF/DOCX/TXT/JPG/PNG, extracted server-side). `text` wins
        when both are present.
    Returns {doc_id, chunks_created, objective_id}.
    """
    content_type = request.headers.get("content-type", "")
    note_text = ""
    source_file = "pasted_notes"

    if content_type.startswith("application/json"):
        body = await request.json()
        subject_id = (body.get("subject_id") or "").strip()
        objective_id = (body.get("objective_id") or "").strip()
        note_text = (body.get("text") or "").strip()
    else:
        form = await request.form()
        subject_id = (form.get("subject_id") or "").strip()
        objective_id = (form.get("objective_id") or "").strip()
        note_text = (form.get("text") or "").strip()
        upload = form.get("file")
        if not note_text and upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File exceeds the 50 MB limit.")
            try:
                note_text = _extract_upload_text(data, upload.filename or "")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            source_file = upload.filename or "uploaded_file"

    if not subject_id or not objective_id:
        raise HTTPException(status_code=422, detail="subject_id and objective_id are required.")

    note_text = note_text[:MAX_NOTE_CHARS].strip()
    if not note_text:
        raise HTTPException(status_code=400, detail="No note text was provided.")

    try:
        return save_notes(
            request.app.state.db, subject_id, objective_id, note_text,
            source_file=source_file, embed_fn=ollama_embed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Upload Material (session 1: stage + extract-for-preview, no ingestion)
# ---------------------------------------------------------------------------
# Errors on these routes follow the {"ok": False, "error": "..."} contract; the
# HTTP status is set via the injected Response so the body shape stays uniform
# (the Stage 12 feedback pattern), rather than HTTPException's {"detail": ...}.
_UPLOAD_EXT_TO_TYPE = {
    ".pdf": "pdf", ".docx": "docx",
    ".png": "image", ".jpg": "image", ".jpeg": "image",  # session 2: image OCR
}


def _run_extraction(db: sqlite3.Connection, staging_id: int) -> None:
    """Background-task wrapper around uploads.extract_text. uploads.extract_text
    already records 'failed' on its own; this guard only catches anything that
    escapes it so a background error never goes silently unlogged."""
    try:
        uploads.extract_text(staging_id, db)
    except Exception:  # noqa: BLE001 -- background task; just log
        logger.exception("Background extraction failed for staging_id=%s", staging_id)


@app.post("/api/upload")
async def upload_material(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    subject_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Stage one PDF/DOCX and kick off background text extraction.

    Validates the subject is locked and the extension is .pdf/.docx, caps the
    file at 50 MB, writes it to the SSD staging area, then returns immediately
    with the staging_id while extraction runs in the background.
    """
    db = request.app.state.db

    if not db.execute(
        "SELECT 1 FROM subjects WHERE subject_id = ? AND syllabus_locked = 1",
        (subject_id,),
    ).fetchone():
        response.status_code = 400
        return {"ok": False, "error": f"Subject '{subject_id}' is not a locked subject."}

    ext = Path(file.filename or "").suffix.lower()
    file_type = _UPLOAD_EXT_TO_TYPE.get(ext)
    if file_type is None:
        response.status_code = 400
        return {
            "ok": False,
            "error": f"Unsupported file type '{ext or file.filename}'. "
                     "Accepted: .pdf, .docx, .png, .jpg, .jpeg.",
        }

    data = await file.read()
    if not data:
        response.status_code = 400
        return {"ok": False, "error": "The uploaded file is empty."}
    if len(data) > MAX_UPLOAD_BYTES:
        response.status_code = 413
        return {"ok": False, "error": "File is too large. Maximum 50 MB."}

    try:
        staging_id = uploads.stage_file(
            db, subject_id, file.filename or f"upload.{file_type}", data, file_type
        )
    except ValueError as exc:
        response.status_code = 400
        return {"ok": False, "error": str(exc)}
    except IOError as exc:
        response.status_code = 500
        return {"ok": False, "error": str(exc)}

    background_tasks.add_task(_run_extraction, db, staging_id)
    return {"ok": True, "staging_id": staging_id, "extract_status": "pending"}


@app.get("/api/staging/{subject_id}")
def staging_list(subject_id: str, request: Request) -> dict:
    """List staged files for a subject, newest first (no full text)."""
    items = uploads.get_staging_list(request.app.state.db, subject_id)
    return {"ok": True, "items": items}


@app.get("/api/staging/{subject_id}/{staging_id}")
def staging_detail(subject_id: str, staging_id: int,
                   request: Request, response: Response) -> dict:
    """Full detail for one staged file, INCLUDING the extracted text and the
    session-2 OCR / truncation signals."""
    db = request.app.state.db
    row = uploads.get_staging_detail(db, staging_id)
    if row is None or row["subject_id"] != subject_id:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}
    chunk_count = uploads.count_chunks(db, staging_id)
    return {
        "ok": True,
        "staging_id": row["staging_id"],
        "original_name": row["original_name"],
        "file_type": row["file_type"],
        "extract_status": row["extract_status"],
        "extract_error": row["extract_error"],
        "extracted_text": row["extracted_text"],
        "status": row["status"],
        "created_at": row["created_at"],
        "ocr_used": bool(row["ocr_used"]),
        "ocr_pages_count": row["ocr_pages_count"] or 0,
        "ocr_confidence_avg": row["ocr_confidence_avg"],
        "total_pages": row["total_pages"],
        "truncated": bool(row["truncated"]),
        "ocr_dpi_reduced": bool(row["ocr_dpi_reduced"]),
        "has_chunks": chunk_count > 0,
        "chunk_count": chunk_count,
    }


@app.delete("/api/staging/{subject_id}/{staging_id}")
def staging_delete(subject_id: str, staging_id: int,
                   request: Request, response: Response) -> dict:
    """Reject / cancel: remove the staged file from the SSD and delete the row."""
    db = request.app.state.db
    row = uploads.get_staging_detail(db, staging_id)
    if row is None or row["subject_id"] != subject_id:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}

    stored = row.get("stored_path")
    if stored:
        try:
            Path(stored).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not delete staged file %s: %s", stored, exc)

    db.execute("DELETE FROM upload_staging WHERE staging_id = ?", (staging_id,))
    db.commit()
    return {"ok": True}


@app.post("/api/staging/{staging_id}/reextract")
def staging_reextract(staging_id: int, request: Request, response: Response,
                      background_tasks: BackgroundTasks) -> dict:
    """Re-run extraction on one staged file (session-2 OCR fallback / chunking) without
    re-uploading. Resets the row to 'pending', drops any chunks, and queues extraction.
    409 while the file is mid-extraction; 404 if it doesn't exist."""
    db = request.app.state.db
    row = db.execute(
        "SELECT extract_status FROM upload_staging WHERE staging_id = ?", (staging_id,)
    ).fetchone()
    if row is None:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}
    if row["extract_status"] == "extracting":
        response.status_code = 409
        return {"ok": False, "error": "File is currently extracting; try again shortly."}

    uploads.reset_for_reextract(db, staging_id)
    background_tasks.add_task(_run_extraction, db, staging_id)
    return {"ok": True, "staging_id": staging_id, "extract_status": "pending"}


@app.post("/api/staging/{subject_id}/reextract-all")
async def staging_reextract_all(subject_id: str, request: Request,
                                background_tasks: BackgroundTasks) -> dict:
    """Bulk re-extract the subject's files that were staged before session-2 logic
    existed (extract_status='ready' AND ocr_used=0 AND total_pages IS NULL).

    With body {"only_low_quality": true}, restrict to files whose extracted_text
    averages below FILE_AVG_THRESHOLD chars per page -- the scanned/OCR candidates --
    so the clean digital files are left untouched."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty/non-JSON body is fine
        body = {}
    only_low_quality = bool(body.get("only_low_quality")) if isinstance(body, dict) else False

    rows = db.execute(
        "SELECT staging_id, extracted_text FROM upload_staging "
        "WHERE subject_id = ? AND extract_status = 'ready' "
        "AND ocr_used = 0 AND total_pages IS NULL",
        (subject_id,),
    ).fetchall()

    selected = []
    for r in rows:
        if only_low_quality:
            text = r["extracted_text"] or ""
            pages = text.count("[Page ")
            # No PDF page markers -> not a scanned-PDF OCR candidate (e.g. a DOCX,
            # which is digital text with no pages). Don't OCR it.
            if pages == 0:
                continue
            if (len(text) / pages) >= uploads.FILE_AVG_THRESHOLD:
                continue
        selected.append(r["staging_id"])

    for sid in selected:
        uploads.reset_for_reextract(db, sid)
        background_tasks.add_task(_run_extraction, db, sid)

    return {"ok": True, "queued": len(selected), "staging_ids": selected}
