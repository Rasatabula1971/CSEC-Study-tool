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
import subprocess
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
import app_state as app_state_store  # noqa: E402  -- avoid clashing with app.state
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
FIRST_LAUNCH_HTML = STATIC_DIR / "first_launch.html"
BUILDER_HTML = STATIC_DIR / "builder.html"
CHAT_HTML = STATIC_DIR / "chat.html"
QUIZ_HTML = STATIC_DIR / "quiz.html"
PLAN_HTML = STATIC_DIR / "study_plan.html"
UPLOAD_HTML = STATIC_DIR / "upload.html"
LESSON_STATUS_HTML = STATIC_DIR / "lesson_status.html"


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
    # Crash-safety for unclean shutdowns. The lifespan's `finally: db.close()` does
    # NOT run on a taskkill /F (Windows TerminateProcess kills the process without
    # unwinding Python), so durability must come from SQLite itself, not cleanup code.
    #   journal_mode=WAL  -- committed transactions live in the -wal file; on the next
    #     open SQLite recovers automatically, and the DB is never left corrupted by a
    #     mid-write process kill (unlike a torn rollback journal). WAL is a persistent
    #     property stored in the DB header, so this one call converts the file for good.
    #   synchronous=NORMAL -- the recommended WAL companion: committed data survives an
    #     application crash (process kill), and the DB stays consistent even on an OS
    #     crash/power loss (at worst the very last transaction is lost, never corruption).
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
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
    # m015 (Upload session 3): classification fields on upload_staging + the
    # upload_classifications table that links each staged file to Gemini's proposed
    # folder + objectives, and records the human review decision. Three new
    # upload_staging columns (all brand-new under m015, so the bundled ALTERs never
    # hit a duplicate-column abort; _run_migration records them [pre-existing] only
    # if a partial earlier run already added one). The one-time skip backfill lives
    # in Layer 2 below so the test pattern (seed rows, then migrate) flags them.
    _run_migration(
        db, "m015_upload_session_3",
        "Classification fields and table for session 3",
        """
        ALTER TABLE upload_staging ADD COLUMN skip_classification INTEGER DEFAULT 0;
        ALTER TABLE upload_staging ADD COLUMN skip_reason TEXT;
        ALTER TABLE upload_staging ADD COLUMN classification_status TEXT
            DEFAULT 'unclassified';
        CREATE TABLE IF NOT EXISTS upload_classifications (
            classification_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            staging_id             INTEGER NOT NULL UNIQUE
                                   REFERENCES upload_staging(staging_id) ON DELETE CASCADE,
            recommended_folder     TEXT NOT NULL CHECK (recommended_folder IN
                                     ('00_SYLLABUS','01_SPECIMEN_PAPERS','02_PAST_PAPERS',
                                      '03_MARK_SCHEMES','04_NOTES','UNCERTAIN')),
            folder_confidence      INTEGER NOT NULL,
            objectives_json        TEXT NOT NULL,
            rationale              TEXT,
            model_used             TEXT NOT NULL,
            raw_response           TEXT,
            classified_at          TEXT DEFAULT (datetime('now')),
            reviewed_at            TEXT,
            review_decision        TEXT,
            review_folder          TEXT,
            review_objectives_json TEXT,
            review_notes           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_classifications_staging
            ON upload_classifications(staging_id);
        CREATE INDEX IF NOT EXISTS idx_classifications_decision
            ON upload_classifications(review_decision)
        """,
    )
    # m016 (Upload session 4): ingestion tracking on upload_staging, stale-lesson
    # flags on objective_lessons, and the ingestion_log audit table. All ALTER columns
    # are brand-new under m016, so the bundled ALTERs never hit a duplicate-column
    # abort (and _run_migration records any as [pre-existing] if a partial run added
    # one already). No data backfill -- defaults ('not_started' / is_stale 0) are the
    # correct initial state for every existing row.
    _run_migration(
        db, "m016_upload_session_4",
        "Ingestion tracking and stale lesson flags",
        """
        ALTER TABLE upload_staging ADD COLUMN ingested_at TEXT;
        ALTER TABLE upload_staging ADD COLUMN ingestion_status TEXT
            DEFAULT 'not_started';
        ALTER TABLE upload_staging ADD COLUMN ingestion_error TEXT;
        ALTER TABLE upload_staging ADD COLUMN ingested_doc_id TEXT
            REFERENCES documents(doc_id);
        ALTER TABLE objective_lessons ADD COLUMN is_stale INTEGER DEFAULT 0;
        ALTER TABLE objective_lessons ADD COLUMN stale_reason TEXT;
        ALTER TABLE objective_lessons ADD COLUMN staled_at TEXT;
        CREATE TABLE IF NOT EXISTS ingestion_log (
            log_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            staging_id       INTEGER NOT NULL REFERENCES upload_staging(staging_id),
            started_at       TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at      TEXT,
            success          INTEGER,
            chunks_created   INTEGER,
            objectives_hit   TEXT,
            lessons_staled   TEXT,
            error_message    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ingestion_log_staging
            ON ingestion_log(staging_id, started_at DESC)
        """,
    )

    # m017 (UI overhaul session 1): app-level singleton state + a retry flag on
    # study_sessions. app_state is a generic key-value table (sticky subject +
    # welcome-seen flag) -- appropriate for a single-student, no-accounts app.
    # is_retry distinguishes a re-attempt (1) from the first try (0) so the original
    # attempt is preserved in history while the retry overwrites the visible result.
    # On a DB built from the updated schema.sql these already exist, so the bundled
    # ALTER raises 'duplicate column name' and _run_migration records m017
    # [pre-existing]; on the live E: DB (predates both) they are created here.
    _run_migration(
        db, "m017_ui_overhaul_state",
        "App-level state: subject preference, welcome-seen flag; study_sessions.is_retry",
        """
        CREATE TABLE IF NOT EXISTS app_state (
            key         TEXT PRIMARY KEY,
            value       TEXT,
            updated_at  TEXT DEFAULT (datetime('now'))
        );
        ALTER TABLE study_sessions ADD COLUMN is_retry INTEGER DEFAULT 0
        """,
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

    # Upload session 3: auto-skip staged files that are not worth classifying --
    # low-OCR-confidence scans, reduced-DPI scans, truncated files, content
    # duplicates, and PDF/DOCX format twins (DOCX preferred, its text is cleaner).
    # Runs on EVERY call (Layer 2) rather than once with m015, so the test pattern
    # (insert rows, then call apply_runtime_migrations to flag them) works and so a
    # newly staged duplicate gets caught. Every UPDATE is idempotent: the first sets
    # skip_classification=1 outright (re-asserting the same skip), the rest are guarded
    # by skip_classification=0 so reasons never accumulate. A file the user later
    # /unskips becomes eligible until the next startup re-asserts a genuine quality
    # skip -- intentional: the quality signal is intrinsic to the file. Wrapped so a
    # pre-m015 DB (columns absent) degrades quietly.
    try:
        db.execute(
            "UPDATE upload_staging "
            "SET skip_classification = 1, skip_reason = 'low_ocr_confidence' "
            "WHERE extract_status = 'ready' AND ocr_used = 1 "
            "  AND ocr_confidence_avg < 70"
        )
        db.execute(
            "UPDATE upload_staging "
            "SET skip_classification = 1, "
            "    skip_reason = COALESCE(skip_reason || ',', '') || 'ocr_dpi_reduced' "
            "WHERE extract_status = 'ready' AND ocr_dpi_reduced = 1 "
            "  AND skip_classification = 0"
        )
        db.execute(
            "UPDATE upload_staging "
            "SET skip_classification = 1, "
            "    skip_reason = COALESCE(skip_reason || ',', '') || 'truncated' "
            "WHERE extract_status = 'ready' AND truncated = 1 "
            "  AND skip_classification = 0"
        )
        # Content duplicates: same extracted_text length (>1000 chars to avoid tiny
        # empties matching), keep the lowest staging_id, skip the rest.
        db.execute(
            """
            UPDATE upload_staging
            SET skip_classification = 1,
                skip_reason = COALESCE(skip_reason || ',', '') || 'duplicate_content'
            WHERE staging_id IN (
                SELECT s2.staging_id
                FROM upload_staging s1
                JOIN upload_staging s2
                  ON LENGTH(s1.extracted_text) = LENGTH(s2.extracted_text)
                 AND LENGTH(s1.extracted_text) > 1000
                 AND s1.staging_id < s2.staging_id
                 AND s1.subject_id = s2.subject_id
                WHERE s1.subject_id = 'Principles_of_Business'
            )
            AND skip_classification = 0
            """
        )
        # Format twins: same filename stem (case-insensitive), keep the DOCX, skip
        # the PDF -- DOCX text extraction is cleaner than from a PDF.
        db.execute(
            """
            UPDATE upload_staging
            SET skip_classification = 1,
                skip_reason = COALESCE(skip_reason || ',', '') || 'format_twin'
            WHERE staging_id IN (
                SELECT s_pdf.staging_id
                FROM upload_staging s_docx
                JOIN upload_staging s_pdf
                  ON LOWER(REPLACE(s_docx.original_name, '.docx', ''))
                   = LOWER(REPLACE(s_pdf.original_name, '.pdf', ''))
                 AND s_docx.file_type = 'docx'
                 AND s_pdf.file_type = 'pdf'
                 AND s_docx.subject_id = s_pdf.subject_id
                WHERE s_docx.subject_id = 'Principles_of_Business'
            )
            AND skip_classification = 0
            """
        )
        db.commit()
    except sqlite3.OperationalError:
        pass  # pre-m015 DB -- classification columns absent; nothing to backfill

    # m018: visual_pages table — tracks one generated HTML visual per objective,
    # cached on the SSD. file_path is absolute so serving is a direct FileResponse.
    # generation_ms is informational; the UI shows a spinner during generation.
    _run_migration(
        db, "m018_visual_pages",
        "visual_pages table for on-demand lesson visuals",
        """
        CREATE TABLE IF NOT EXISTS visual_pages (
            objective_id  TEXT PRIMARY KEY REFERENCES objectives(objective_id),
            subject_id    TEXT NOT NULL REFERENCES subjects(subject_id),
            generated_at  TEXT NOT NULL,
            model_used    TEXT NOT NULL DEFAULT 'gemini',
            file_path     TEXT NOT NULL,
            generation_ms INTEGER
        )
        """,
    )

    # m020: point_group_id on mark_points — shared key that ties fanned-out rows
    # (one per objective from a multi-objective CSV row) back to one gradeable point.
    # Single-objective rows carry point_group_id = their own positional key; NULL on
    # legacy rows (grade.py treats NULL as a unique group, i.e. no dedup needed).
    _run_migration(db, "m020_mark_points_point_group_id",
                   "mark_points.point_group_id",
                   "ALTER TABLE mark_points ADD COLUMN point_group_id TEXT")

    # m019: objective_videos table — pre-qualified YouTube links keyed to objectives,
    # loaded by backend/load_video_links.py (PHASE: build). Runtime path reads only.
    _run_migration(
        db, "m019_objective_videos",
        "objective_videos table for pre-qualified YouTube links",
        """
        CREATE TABLE IF NOT EXISTS objective_videos (
            video_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            objective_id TEXT NOT NULL REFERENCES objectives(objective_id),
            subject_id   TEXT NOT NULL REFERENCES subjects(subject_id),
            url          TEXT NOT NULL,
            title        TEXT NOT NULL,
            channel      TEXT,
            duration_str TEXT,
            source_file  TEXT NOT NULL,
            added_at     TEXT DEFAULT (datetime('now')),
            UNIQUE(objective_id, url)
        )
        """,
    )

    # Upload session 1: make sure the SSD staging tree exists for every locked
    # subject. Best-effort -- a missing SSD just logs a warning here and surfaces a
    # clearer error at upload time.
    ensure_staging_dirs(db)
    ensure_visual_dirs(db)


def ensure_visual_dirs(db: sqlite3.Connection) -> None:
    """Create {SSD_ROOT}/05_VISUALS and a subdir per locked subject.

    Best-effort — warns and skips if the SSD is not mounted.
    """
    ssd_root = os.getenv("SSD_ROOT")
    if not ssd_root:
        return
    visuals_root = Path(ssd_root) / "05_VISUALS"
    try:
        visuals_root.mkdir(parents=True, exist_ok=True)
        rows = db.execute(
            "SELECT subject_id FROM subjects WHERE syllabus_locked = 1"
        ).fetchall()
        for r in rows:
            (visuals_root / r["subject_id"]).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create visual dirs under %s (%s)", visuals_root, exc
        )


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


async def _background_prewarm() -> None:
    """Load the chat model into Ollama's RAM after routes start serving.

    Runs as an asyncio task scheduled just before the lifespan yields, so
    launch.bat's /health poll succeeds immediately rather than waiting up to
    120 s for the cold model load. If the pre-warm fails, the first real
    student request pays the cold-load cost instead — acceptable trade-off.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None,
            lambda: ollama_chat(
                [{"role": "user", "content": "ready"}],
                system="Respond with one word: ready.",
            ),
        )
        logger.info("Ollama pre-warm complete — chat model is resident.")
    except Exception as exc:
        logger.warning("Ollama pre-warm failed (%s) -- first response may be slow.", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio as _asyncio

    ssd_root = os.getenv("SSD_ROOT")
    if ssd_root and not os.path.exists(ssd_root):
        sys.exit(f"ERROR: SSD not mounted at {ssd_root}. Plug in the drive and restart.")

    if ollama_health():
        # Schedule pre-warm as a background task so it runs AFTER yield.
        # Previously this was a blocking call here, which delayed the lifespan
        # yield by up to 120 s on a cold model load — long enough for launch.bat's
        # /health polling loop to time out before routes were even served.
        _asyncio.create_task(_background_prewarm())
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
    # UI overhaul session 1: a grade turn sets this on a re-attempt of a recall
    # question so the controller flags the study_sessions row and overwrites the
    # Leitner decision while keeping the first attempt in history.
    is_retry: bool = False


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
    # UI overhaul session 3: a recall-question retry sets this so the per-objective
    # study_sessions row is flagged is_retry=1 (session 1's scoring), while the
    # original attempt's row is preserved.
    is_retry: bool = False


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


class ClassifyAllRequest(BaseModel):
    """Trigger a bulk classification run for a subject (upload session 3)."""
    force: bool = False


class IngestAllRequest(BaseModel):
    """Trigger ingestion of every accepted file for a subject (upload session 4).
    dry_run reports what would happen without moving files or touching the DB."""
    dry_run: bool = False


class BackupRequest(BaseModel):
    """Take a labelled DB backup (build-time safety net, triggerable from the UI)."""
    label: str = "manual"


class ReviewObjective(BaseModel):
    """One objective in an override decision -- only the id is needed."""
    objective_id: str


class ReviewRequest(BaseModel):
    """A human review decision on a file's classification (upload session 3).

    decision is a Literal, so an unknown value is rejected with 422 before the
    endpoint body runs. override_folder / override_objectives are only meaningful
    when decision='overridden'.
    """
    decision: Literal['accepted', 'overridden', 'rejected']
    override_folder: Optional[str] = None
    override_objectives: Optional[list[ReviewObjective]] = None
    notes: Optional[str] = None


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


class SubjectStateRequest(BaseModel):
    """Set the sticky subject (UI overhaul session 1). Validated against
    syllabus_locked at the store layer -- an unlocked/unknown subject -> 400."""
    subject_id: str = Field(min_length=1)


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


@app.get("/lessons/status")
def lesson_status_page() -> FileResponse:
    """Serve the read-only lesson-generation status page (auto-refreshing)."""
    return FileResponse(LESSON_STATUS_HTML)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def index(request: Request) -> FileResponse:
    """The app's front door (UI overhaul session 2).

    On the FIRST launch ever -- before the welcome message has been marked seen --
    serve the one-time first-launch message (first_launch.html). On every launch
    after that, serve the redesigned Welcome page. The check is server-side via
    app_state (has_seen_welcome_message), so there is no client-side flash of the
    wrong page. The previous chat UI remains reachable at /chat."""
    db = request.app.state.db
    if not app_state_store.has_seen_welcome_message(db):
        return FileResponse(FIRST_LAUNCH_HTML)
    return FileResponse(WELCOME_HTML)


@app.get("/chat")
def chat_page() -> FileResponse:
    """The chat UI, reachable at /chat (kept for existing bookmarks)."""
    return FileResponse(CHAT_HTML)


@app.get("/builder")
def builder_page() -> FileResponse:
    """The Builder console (UI overhaul session 3): a minimal utility page linking the
    staging tool + lesson-status page and exposing the reset-welcome action. Entry is
    PIN-gated client-side (the session-2 modal on Welcome navigates here after a correct
    PIN; the page itself re-checks via /api/builder/verify-pin on direct navigation)."""
    return FileResponse(BUILDER_HTML)


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
        JOIN   chunks c ON c.chunk_id = mp.question_id
        JOIN   documents d ON d.doc_id = c.doc_id
        WHERE  c.subject_id = ?
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
        "  AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')",
        "  AND  c.question_num IS NOT NULL",
        # Only show solution-derived questions (chunk_id like 'POB-...-stem',
        # the ingest_solutions.py convention) or specimen stems ingested by
        # ingest_econ_specimen_stems.py. ingest.py's MCQ chunker still
        # produces garbled chunks for older Paper 2 PDFs -- missing stems,
        # options split across chunks, OCR artifacts ("U nski lied") -- so its
        # auto-generated chunk_ids are excluded until the chunker is rewritten.
        "  AND  c.chunk_id LIKE '%-stem'",
        "  AND  c.page IS NOT NULL",
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
          AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')
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
          AND  d.content_type IN ('past_paper', 'mark_scheme', 'specimen')
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


@app.get("/api/objectives/{subject_id}/map")
def objectives_map(subject_id: str, request: Request) -> dict:
    """Objective map (UI overhaul session 3): objectives grouped by syllabus section,
    each tagged with a status the Study page colours as a dot.

    Status reuses the SAME mastery model as get_plan_progress (the "X of Y mastered"
    header): an objective is 'mastered' exactly when its study_plan.status is
    'mastered' (passed on two distinct days). Otherwise 'attempted' if it has any
    study_sessions row, else 'not_started'. is_next_due flags objectives in the
    scheduler's due-today set (get_due_objectives) so the map can highlight them amber.

    Counting map 'mastered' rows therefore yields exactly get_plan_progress()['mastered'].
    """
    db = request.app.state.db
    rows = db.execute(
        """
        SELECT o.objective_id, o.section_id, o.objective_num, o.content_stmt,
               s.title       AS section_title,
               s.section_num AS section_num,
               sp.status     AS plan_status,
               EXISTS(SELECT 1 FROM study_sessions ss
                      WHERE ss.objective_id = o.objective_id) AS attempted
        FROM   objectives o
        JOIN   syllabus_sections s ON s.section_id = o.section_id
        LEFT   JOIN study_plan sp
               ON sp.objective_id = o.objective_id AND sp.subject_id = o.subject_id
        WHERE  o.subject_id = ?
        ORDER  BY s.section_num, o.objective_num
        """,
        (subject_id,),
    ).fetchall()

    due_ids = {r["objective_id"] for r in get_due_objectives(db, subject_id)}

    sections: list[dict] = []
    by_section: dict[str, dict] = {}
    for r in rows:
        if r["plan_status"] == "mastered":
            status = "mastered"
        elif r["attempted"]:
            status = "attempted"
        else:
            status = "not_started"
        sec = by_section.get(r["section_id"])
        if sec is None:
            sec = {
                "section_id": r["section_id"],
                "section_num": r["section_num"],
                "title": r["section_title"],
                "objectives": [],
            }
            by_section[r["section_id"]] = sec
            sections.append(sec)
        sec["objectives"].append({
            "objective_id": r["objective_id"],
            "objective_num": r["objective_num"],
            "content_stmt": r["content_stmt"],
            "status": status,
            "is_next_due": r["objective_id"] in due_ids,
        })
    return {"sections": sections}


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


def _safe_json(raw, default):
    """Decode a JSON-text column to Python, returning `default` on null/bad JSON."""
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


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


@app.get("/api/objective/{objective_id}")
def objective_lesson(objective_id: str, request: Request) -> dict:
    """One objective's lesson + recall questions, for the /plan 'Jump to objective'
    input. The objective's subject_id is looked up from the objectives table (the
    caller supplies only the objective_id), then the request is routed through the
    SAME controller teach path the batch loader uses (route='teach'), so a stored
    canonical lesson is served deterministically and an objective with none returns
    the existing placeholder contract (lesson_source='placeholder', recall_questions=[],
    source_file=None, page=None, context_source='syllabus'). 404 if the id is unknown.
    """
    db = request.app.state.db
    row = db.execute(
        "SELECT subject_id FROM objectives WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="objective not found")
    req = {
        "route": "teach",
        "subject_id": row["subject_id"],
        "objective_id": objective_id,
        "query": "Teach me this objective",
    }
    return _shape_for_ui(handle_request(db, req))


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
# App-level state (UI overhaul session 1): sticky subject + first-launch flag
# ---------------------------------------------------------------------------
@app.get("/api/state/subject")
def get_subject_state(request: Request) -> dict:
    """The sticky subject_id (defaults to the first locked subject if unset)."""
    return {"subject_id": app_state_store.get_current_subject(request.app.state.db)}


@app.post("/api/state/subject")
def set_subject_state(body: SubjectStateRequest, request: Request,
                      response: Response) -> dict:
    """Persist the sticky subject. 400 if it does not exist or is not locked."""
    try:
        app_state_store.set_current_subject(request.app.state.db, body.subject_id)
    except ValueError as exc:
        response.status_code = 400
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "subject_id": body.subject_id}


@app.get("/api/state/welcome-seen")
def get_welcome_seen(request: Request) -> dict:
    """Whether the first-launch welcome message has been shown."""
    return {"seen": app_state_store.has_seen_welcome_message(request.app.state.db)}


@app.post("/api/state/welcome-seen")
def set_welcome_seen(request: Request) -> dict:
    """Mark the first-launch welcome message seen (one-way; no body needed)."""
    app_state_store.mark_welcome_message_seen(request.app.state.db)
    return {"ok": True}


@app.post("/api/state/welcome-reset")
def reset_welcome_seen(request: Request) -> dict:
    """Flip welcome_message_seen back to '0' (UI overhaul session 3, Builder console).

    Lets the builder re-arm the one-time first-launch message -- e.g. setting the app
    up fresh for a sibling, or after a DB restore. Builder-only (the link sits behind
    the PIN gate); never exposed to the student."""
    app_state_store.set_state(request.app.state.db, "welcome_message_seen", "0")
    return {"ok": True}


@app.post("/api/builder/verify-pin")
async def builder_verify_pin(request: Request) -> dict:
    """Check the builder PIN for the Welcome page's discreet Builder link.

    A deliberately light gate for a single-student local app: it only keeps the
    student out of the builder console by accident. The PIN is checked SERVER-SIDE
    against BUILDER_PIN in .env (default '1971') so the value never ships to the
    browser. Returns {"ok": true|false}; the front end counts its own wrong attempts.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- missing/non-JSON body counts as a wrong PIN
        body = {}
    pin = str(body.get("pin", "")) if isinstance(body, dict) else ""
    expected = os.getenv("BUILDER_PIN", "1971")
    return {"ok": pin == expected}


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
        "is_retry": body.is_retry,
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

# Student-upload (UI overhaul session 2) -- the simple drop-a-file affordance on the
# Welcome page. Auto-accept only when the placement is unambiguous: a confident folder
# AND exactly one high-confidence objective. Anything else is left for the builder's
# existing review queue (never a confidence score shown to the student).
STUDENT_UPLOAD_AUTO_FOLDER_CONF = 85
STUDENT_UPLOAD_AUTO_OBJ_CONF = 85
_STUDENT_UPLOAD_ERROR_MSG = (
    "Something went wrong — try again, or ask your dad to check it later."
)
# Plain-language names for the CXC archive folders, for the "Added to ___" message.
_FOLDER_DISPLAY = {
    "00_SYLLABUS": "Syllabus",
    "01_SPECIMEN_PAPERS": "Specimen Papers",
    "02_PAST_PAPERS": "Past Papers",
    "03_MARK_SCHEMES": "Mark Schemes",
    "04_NOTES": "Notes",
    "UNCERTAIN": "your review pile",
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


@app.post("/api/student-upload")
async def student_upload(
    request: Request,
    subject_id: Optional[str] = Form(None),
    file: UploadFile = File(...),
) -> dict:
    """Student-facing single-file upload (UI overhaul session 2).

    Deliberately SEPARATE from the builder's /api/upload staging workflow: the
    student just drops one file and gets an automatic outcome -- no review queue, no
    confidence scores, no technical detail surfaced to her. The flow is synchronous
    (a single photo/worksheet extracts in seconds, unlike the builder's bulk runs):

      stage (uploads.stage_file) -> extract (uploads.extract_text) ->
      classify (classify_uploads.single_file_classify, Gemini at build-phase) -> decide.

    Decision:
      * folder_confidence >= 85 AND exactly one high-confidence objective -> auto-accept
        and ingest this one file (upload_ingest.ingest_staged_file). -> outcome 'added'.
      * low confidence / ambiguous classification -> leave it staged, unreviewed, for
        the builder's existing /upload queue (review_notes='pending_student_upload_review').
        -> outcome 'needs_review'.
      * a hard failure (bad type, empty, extraction failed, classification failed) ->
        outcome 'error' with a short, non-technical message; the real error is logged
        server-side, never returned. The file (if staged) still surfaces to the builder.

    Always returns HTTP 200; the {ok, outcome} body drives the UI so the front-end
    never has to branch on status codes. `subject_id` falls back to the sticky state.
    """
    db = request.app.state.db

    subject = subject_id or app_state_store.get_current_subject(db)
    if not subject or not db.execute(
        "SELECT 1 FROM subjects WHERE subject_id = ? AND syllabus_locked = 1",
        (subject,),
    ).fetchone():
        return {"ok": False, "outcome": "error",
                "message": "That subject isn't ready yet — ask your dad."}

    ext = Path(file.filename or "").suffix.lower()
    file_type = _UPLOAD_EXT_TO_TYPE.get(ext)
    if file_type is None:
        return {"ok": False, "outcome": "error",
                "message": "That file type isn't supported. Try a PDF, a photo, or a Word document."}

    data = await file.read()
    if not data:
        return {"ok": False, "outcome": "error",
                "message": "That file looks empty. Try choosing it again."}
    if len(data) > MAX_UPLOAD_BYTES:
        return {"ok": False, "outcome": "error",
                "message": "That file is too big (the limit is 50 MB)."}

    staging_id = None
    try:
        # 1-2. Stage to the SSD, then extract synchronously (small single file).
        staging_id = uploads.stage_file(
            db, subject, file.filename or f"upload.{file_type}", data, file_type
        )
        uploads.extract_text(staging_id, db)
        srow = db.execute(
            "SELECT extract_status FROM upload_staging WHERE staging_id = ?",
            (staging_id,),
        ).fetchone()
        if not srow or srow["extract_status"] != "ready":
            # Extraction did not yield usable text; the file is saved, but there is
            # nothing to classify on. Surface a quiet error -- the builder can re-extract.
            logger.warning("student-upload extraction not ready for staging_id=%s", staging_id)
            return {"ok": False, "outcome": "error", "message": _STUDENT_UPLOAD_ERROR_MSG}

        # 3. Classify (lazy import keeps the cloud client off the runtime import path).
        import classify_uploads
        classification = classify_uploads.single_file_classify(db, staging_id)
    except Exception:  # noqa: BLE001 -- log the real cause, never leak it to the student
        logger.exception("student-upload failed for %r (staging_id=%s)",
                         file.filename, staging_id)
        return {"ok": False, "outcome": "error", "message": _STUDENT_UPLOAD_ERROR_MSG}

    if not classification or classification.get("classification_status") == "failed":
        # Gemini unreachable / unparseable after retries. The file is staged and shows
        # up in the builder's queue; tell the student it didn't sort, without detail.
        if staging_id is not None:
            db.execute(
                "UPDATE upload_classifications SET review_notes = "
                "'pending_student_upload_review' WHERE staging_id = ?", (staging_id,))
            db.commit()
        return {"ok": False, "outcome": "error", "message": _STUDENT_UPLOAD_ERROR_MSG}

    folder = classification["recommended_folder"]
    folder_conf = classification["folder_confidence"] or 0
    high_objs = [
        o for o in classification["objectives"]
        if (o.get("confidence") or 0) >= STUDENT_UPLOAD_AUTO_OBJ_CONF
    ]

    if folder_conf >= STUDENT_UPLOAD_AUTO_FOLDER_CONF and len(high_objs) == 1:
        # 4. Confident, unambiguous placement -> accept + ingest this one file.
        try:
            db.execute(
                "UPDATE upload_classifications SET review_decision = 'accepted', "
                "reviewed_at = datetime('now'), "
                "review_notes = 'auto_accepted_student_upload' WHERE staging_id = ?",
                (staging_id,),
            )
            db.commit()
            import upload_ingest
            upload_ingest.ingest_staged_file(db, staging_id)
        except Exception:  # noqa: BLE001 -- ingestion failed; keep the file for review
            logger.exception("student-upload ingest failed for staging_id=%s", staging_id)
            db.execute(
                "UPDATE upload_classifications SET review_decision = NULL, "
                "review_notes = 'pending_student_upload_review' WHERE staging_id = ?",
                (staging_id,),
            )
            db.commit()
            return {"ok": True, "outcome": "needs_review"}
        return {"ok": True, "outcome": "added",
                "section": _FOLDER_DISPLAY.get(folder, folder)}

    # 5. Low confidence / ambiguous -> leave it for the builder's review queue.
    db.execute(
        "UPDATE upload_classifications SET review_decision = NULL, "
        "review_notes = 'pending_student_upload_review' WHERE staging_id = ?",
        (staging_id,),
    )
    db.commit()
    return {"ok": True, "outcome": "needs_review"}


@app.get("/api/staging/{subject_id}")
def staging_list(subject_id: str, request: Request) -> dict:
    """List staged files for a subject, newest first (no full text)."""
    items = uploads.get_staging_list(request.app.state.db, subject_id)
    return {"ok": True, "items": items}


@app.get("/api/staging/{subject_id}/classifications")
def staging_classifications(subject_id: str, request: Request) -> dict:
    """Staged files joined with their classification proposals (upload session 3).

    Ordering puts the files that need a human decision first: classified-awaiting-
    review, then unclassified, then skipped, then failed; newest first within each
    group. This route is declared BEFORE /{staging_id} so the literal 'classifications'
    is not swallowed by the int path param.
    """
    db = request.app.state.db
    rows = db.execute(
        """
        SELECT s.staging_id, s.original_name, s.extract_status,
               s.skip_classification, s.skip_reason, s.classification_status,
               s.created_at,
               c.recommended_folder, c.folder_confidence, c.objectives_json,
               c.rationale, c.model_used, c.review_decision, c.review_folder,
               c.review_objectives_json
        FROM   upload_staging s
        LEFT   JOIN upload_classifications c ON c.staging_id = s.staging_id
        WHERE  s.subject_id = ?
        ORDER  BY CASE s.classification_status
                    WHEN 'classified'   THEN 0
                    WHEN 'classifying'  THEN 1
                    WHEN 'unclassified' THEN 2
                    WHEN 'queued'       THEN 3
                    WHEN 'skipped'      THEN 4
                    WHEN 'failed'       THEN 5
                    ELSE 6
                  END,
                  s.created_at DESC, s.staging_id DESC
        """,
        (subject_id,),
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        classification = None
        if d["recommended_folder"] is not None:
            classification = {
                "recommended_folder": d["recommended_folder"],
                "folder_confidence": d["folder_confidence"],
                "objectives": _safe_json(d["objectives_json"], []),
                "rationale": d["rationale"],
                "model_used": d["model_used"],
                "review_decision": d["review_decision"],
                "review_folder": d["review_folder"],
                "review_objectives": _safe_json(d["review_objectives_json"], None),
            }
        items.append({
            "staging_id": d["staging_id"],
            "original_name": d["original_name"],
            "extract_status": d["extract_status"],
            "skip_classification": bool(d["skip_classification"]),
            "skip_reason": d["skip_reason"],
            "classification_status": d["classification_status"],
            "classification": classification,
        })
    return {"ok": True, "items": items}


@app.get("/api/staging/{subject_id}/ingestion-status")
def staging_ingestion_status(subject_id: str, request: Request) -> dict:
    """Aggregate ingestion counts + per-file status (upload session 4). Declared BEFORE
    /{staging_id} so the literal 'ingestion-status' is not swallowed by the int param.

    Per-file chunks_created/objectives_hit/lessons_staled come from the most recent
    ingestion_log row for that file."""
    db = request.app.state.db
    rows = db.execute(
        """
        SELECT s.staging_id, s.original_name, s.ingestion_status, s.ingested_at,
               s.ingestion_error,
               l.chunks_created, l.objectives_hit, l.lessons_staled
        FROM   upload_staging s
        LEFT   JOIN ingestion_log l ON l.log_id = (
                 SELECT log_id FROM ingestion_log
                 WHERE staging_id = s.staging_id
                 ORDER BY started_at DESC, log_id DESC LIMIT 1
               )
        WHERE  s.subject_id = ?
        ORDER  BY s.created_at DESC, s.staging_id DESC
        """,
        (subject_id,),
    ).fetchall()

    totals = {"not_started": 0, "queued": 0, "ingesting": 0, "ingested": 0, "failed": 0}
    items = []
    for r in rows:
        st = r["ingestion_status"] or "not_started"
        if st in totals:
            totals[st] += 1
        items.append({
            "staging_id": r["staging_id"],
            "original_name": r["original_name"],
            "ingestion_status": st,
            "ingested_at": r["ingested_at"],
            "chunks_created": r["chunks_created"],
            "objectives_hit": _safe_json(r["objectives_hit"], None),
            "lessons_staled": _safe_json(r["lessons_staled"], None),
            "ingestion_error": r["ingestion_error"],
        })
    return {"ok": True, "totals": totals, "items": items}


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


# ---------------------------------------------------------------------------
# Upload Material (session 3: Gemini classification + human review)
# ---------------------------------------------------------------------------
# classify_uploads is PHASE: build (it pulls in the cloud client transitively via
# the router). It is imported lazily inside the background-task wrapper so the live
# offline server never loads google.generativeai at startup -- only when a build-time
# classification is actually triggered from the UI.
def _run_classification(db: sqlite3.Connection, subject_id: str,
                        staging_id=None, force: bool = False) -> None:
    """Background-task wrapper around classify_uploads.classify_uploads. Lazy import
    keeps the cloud client off the runtime startup path; any error is logged (each
    file's own failure is already recorded as classification_status='failed')."""
    try:
        import classify_uploads
        classify_uploads.classify_uploads(
            db, subject_id, staging_id=staging_id, force=force, verbose=False,
        )
    except Exception:  # noqa: BLE001 -- background task; just log
        logger.exception(
            "Background classification failed (subject=%s, staging_id=%s)",
            subject_id, staging_id,
        )


def _count_eligible_for_classification(db: sqlite3.Connection, subject_id: str,
                                       force: bool) -> int:
    """How many staged files this run will send to the model: ready, not skipped, and
    (unless --force) not already classified. Mirrors classify_uploads._eligible_files."""
    sql = (
        "SELECT COUNT(*) FROM upload_staging "
        "WHERE subject_id = ? AND extract_status = 'ready' AND skip_classification = 0"
    )
    if not force:
        sql += " AND classification_status = 'unclassified'"
    return db.execute(sql, (subject_id,)).fetchone()[0]


@app.post("/api/staging/{subject_id}/classify-all")
async def staging_classify_all(subject_id: str, request: Request,
                               background_tasks: BackgroundTasks) -> dict:
    """Classify every eligible staged file for a subject (background). Returns the
    count queued immediately; the run proceeds in the background. Body (optional):
    {"force": false}."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty/non-JSON body is fine
        body = {}
    force = bool(body.get("force")) if isinstance(body, dict) else False

    queued = _count_eligible_for_classification(db, subject_id, force)
    if queued:
        background_tasks.add_task(_run_classification, db, subject_id, None, force)
    return {"ok": True, "queued": queued}


@app.post("/api/staging/{staging_id}/classify")
def staging_classify_one(staging_id: int, request: Request,
                         response: Response, background_tasks: BackgroundTasks) -> dict:
    """Classify (or re-classify) a single staged file (background). 404 if absent;
    400 if it is auto-skipped (unskip it first)."""
    db = request.app.state.db
    row = db.execute(
        "SELECT subject_id, extract_status, skip_classification "
        "FROM upload_staging WHERE staging_id = ?",
        (staging_id,),
    ).fetchone()
    if row is None:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}
    if row["skip_classification"]:
        response.status_code = 400
        return {"ok": False, "error": "File is auto-skipped; unskip it before classifying."}
    if row["extract_status"] != "ready":
        response.status_code = 400
        return {"ok": False, "error": "File is not extracted yet."}

    # Single-file: always (re)classify on demand -> force=True.
    background_tasks.add_task(_run_classification, db, row["subject_id"], staging_id, True)
    return {"ok": True, "staging_id": staging_id, "classification_status": "queued"}


@app.post("/api/staging/{staging_id}/review")
def staging_review(staging_id: int, body: ReviewRequest,
                   request: Request, response: Response) -> dict:
    """Record a human review decision (accepted / overridden / rejected) against a
    file's classification row. 404 if the file has no classification to review."""
    db = request.app.state.db
    crow = db.execute(
        "SELECT classification_id FROM upload_classifications WHERE staging_id = ?",
        (staging_id,),
    ).fetchone()
    if crow is None:
        response.status_code = 404
        return {"ok": False, "error": "No classification to review for this file."}

    review_folder = body.override_folder if body.decision == "overridden" else None
    review_objs = None
    if body.decision == "overridden" and body.override_objectives is not None:
        review_objs = json.dumps([{"objective_id": o.objective_id}
                                  for o in body.override_objectives])

    db.execute(
        "UPDATE upload_classifications "
        "SET review_decision = ?, review_folder = ?, review_objectives_json = ?, "
        "    review_notes = ?, reviewed_at = datetime('now') "
        "WHERE staging_id = ?",
        (body.decision, review_folder, review_objs, body.notes, staging_id),
    )
    db.commit()
    return {"ok": True}


@app.post("/api/staging/{staging_id}/unskip")
def staging_unskip(staging_id: int, request: Request, response: Response) -> dict:
    """Clear the auto-skip flag on a staged file so it becomes eligible again.
    Resets classification_status to 'unclassified'. 404 if the file is absent."""
    db = request.app.state.db
    row = db.execute(
        "SELECT 1 FROM upload_staging WHERE staging_id = ?", (staging_id,)
    ).fetchone()
    if row is None:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}

    db.execute(
        "UPDATE upload_staging "
        "SET skip_classification = 0, skip_reason = NULL, "
        "    classification_status = 'unclassified', updated_at = datetime('now') "
        "WHERE staging_id = ?",
        (staging_id,),
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Upload Material (session 4: ingestion + stale-lesson tracking)
# ---------------------------------------------------------------------------
# upload_ingest and ingest_lessons are PHASE: build (they pull the ingest pipeline
# in). Lazy-imported inside the background-task wrappers so the runtime server never
# loads them at startup -- only when a build-time ingestion/regeneration is triggered.
def _run_ingest_one(db: sqlite3.Connection, staging_id: int) -> None:
    """Background wrapper around upload_ingest.ingest_staged_file (one file). Errors are
    already recorded on the staging row + ingestion_log; this just logs anything that
    escapes."""
    try:
        import upload_ingest
        upload_ingest.ingest_staged_file(db, staging_id)
    except Exception:  # noqa: BLE001 -- background task; the failure is already recorded
        logger.exception("Background ingestion failed for staging_id=%s", staging_id)


def _run_ingest_all(db: sqlite3.Connection, subject_id: str) -> None:
    """Background wrapper around upload_ingest.ingest_all_accepted (whole subject)."""
    try:
        import upload_ingest
        upload_ingest.ingest_all_accepted(db, subject_id, dry_run=False)
    except Exception:  # noqa: BLE001 -- background task; just log
        logger.exception("Background bulk ingestion failed for subject=%s", subject_id)


def _run_regenerate(db: sqlite3.Connection, subject_id: str,
                    objective_ids: list) -> None:
    """Background wrapper around upload_ingest.regenerate_lessons (offline Ollama)."""
    try:
        import upload_ingest
        upload_ingest.regenerate_lessons(db, subject_id, objective_ids)
    except Exception:  # noqa: BLE001 -- background task; just log
        logger.exception("Background lesson regeneration failed (subject=%s, objectives=%s)",
                         subject_id, objective_ids)


def _classification_decision(db: sqlite3.Connection, staging_id: int):
    """(review_decision, ingestion_status) for a staged file, or (None, None) if the
    staging row is absent. review_decision is None when the file has no classification."""
    row = db.execute(
        "SELECT s.ingestion_status, c.review_decision "
        "FROM upload_staging s "
        "LEFT JOIN upload_classifications c ON c.staging_id = s.staging_id "
        "WHERE s.staging_id = ?",
        (staging_id,),
    ).fetchone()
    if row is None:
        return None, None
    return row["review_decision"], row["ingestion_status"]


@app.post("/api/staging/{staging_id}/ingest")
def staging_ingest_one(staging_id: int, request: Request,
                       response: Response, background_tasks: BackgroundTasks) -> dict:
    """Ingest one accepted/overridden staged file (background). 404 if absent; 400 if the
    classification is not accepted/overridden or the file is already ingested."""
    db = request.app.state.db
    decision, ing_status = _classification_decision(db, staging_id)
    if decision is None and ing_status is None:
        response.status_code = 404
        return {"ok": False, "error": "Staged file not found."}
    if decision not in ("accepted", "overridden"):
        response.status_code = 400
        return {"ok": False,
                "error": "File classification must be accepted or overridden before ingestion."}
    if ing_status == "ingested":
        response.status_code = 400
        return {"ok": False, "error": "File is already ingested."}

    db.execute(
        "UPDATE upload_staging SET ingestion_status = 'queued' WHERE staging_id = ?",
        (staging_id,),
    )
    db.commit()
    background_tasks.add_task(_run_ingest_one, db, staging_id)
    return {"ok": True, "staging_id": staging_id, "ingestion_status": "queued"}


@app.post("/api/staging/{subject_id}/ingest-all")
async def staging_ingest_all(subject_id: str, request: Request,
                             background_tasks: BackgroundTasks) -> dict:
    """Ingest every accepted/overridden, not-yet-ingested file for a subject (background).
    Body (optional): {"dry_run": false}. Returns the count queued; with dry_run=true it
    reports what would be ingested without touching anything."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty/non-JSON body is fine
        body = {}
    dry_run = bool(body.get("dry_run")) if isinstance(body, dict) else False

    eligible = db.execute(
        """
        SELECT s.staging_id FROM upload_staging s
        JOIN upload_classifications c ON c.staging_id = s.staging_id
        WHERE s.subject_id = ?
          AND c.review_decision IN ('accepted', 'overridden')
          AND s.ingestion_status = 'not_started'
        """,
        (subject_id,),
    ).fetchall()
    queued = len(eligible)

    if dry_run:
        import upload_ingest
        return {"ok": True, "dry_run": True,
                "result": upload_ingest.ingest_all_accepted(db, subject_id, dry_run=True)}

    if queued:
        db.execute(
            "UPDATE upload_staging SET ingestion_status = 'queued' "
            "WHERE staging_id IN (%s)" % ",".join("?" * queued),
            [r[0] for r in eligible],
        )
        db.commit()
        background_tasks.add_task(_run_ingest_all, db, subject_id)
    return {"ok": True, "queued": queued}


@app.get("/api/lessons/stale/{subject_id}")
def lessons_stale(subject_id: str, request: Request) -> dict:
    """Lessons flagged for regeneration, each with the file(s) that caused staleness."""
    import upload_ingest
    return {"ok": True,
            "stale_lessons": upload_ingest.get_stale_lessons(request.app.state.db, subject_id)}


@app.get("/api/lessons/status/{subject_id}")
def lessons_status(subject_id: str, request: Request) -> dict:
    """Read-only live snapshot of lesson generation for a subject (no new state).

    Counts come straight from objective_lessons + lesson_generation_queue.
    queue_by_reason groups on the reason PREFIX (text before the first ':'), so
    'quality_check_failed: pre-existing' and 'quality_check_failed: <why>' collapse to
    'quality_check_failed'. recent_activity unions the three timestamped events
    (lesson_written / staled / queued), newest first, capped at 10.
    """
    db = request.app.state.db

    total_objectives = db.execute(
        "SELECT COUNT(*) FROM objectives WHERE subject_id = ?", (subject_id,)
    ).fetchone()[0]
    lessons_written = db.execute(
        "SELECT COUNT(*) FROM objective_lessons WHERE subject_id = ?", (subject_id,)
    ).fetchone()[0]
    lessons_stale = db.execute(
        "SELECT COUNT(*) FROM objective_lessons WHERE subject_id = ? AND is_stale = 1",
        (subject_id,),
    ).fetchone()[0]

    # The queue table is subject-agnostic (objective_id only) -> join to objectives.
    queue_rows = db.execute(
        """
        SELECT q.reason FROM lesson_generation_queue q
        JOIN   objectives o ON o.objective_id = q.objective_id
        WHERE  o.subject_id = ?
        """,
        (subject_id,),
    ).fetchall()
    lessons_queued = len(queue_rows)
    queue_by_reason: dict = {}
    for r in queue_rows:
        prefix = (r["reason"] or "unknown").split(":", 1)[0].strip()
        queue_by_reason[prefix] = queue_by_reason.get(prefix, 0) + 1

    events = db.execute(
        """
        SELECT objective_id, 'lesson_written' AS event, NULL AS reason,
               generated_at AS timestamp
        FROM   objective_lessons
        WHERE  subject_id = ? AND generated_at IS NOT NULL
        UNION ALL
        SELECT objective_id, 'staled' AS event, stale_reason AS reason,
               staled_at AS timestamp
        FROM   objective_lessons
        WHERE  subject_id = ? AND is_stale = 1 AND staled_at IS NOT NULL
        UNION ALL
        SELECT q.objective_id, 'queued' AS event, q.reason AS reason,
               q.created_at AS timestamp
        FROM   lesson_generation_queue q
        JOIN   objectives o ON o.objective_id = q.objective_id
        WHERE  o.subject_id = ?
        ORDER  BY timestamp DESC
        LIMIT  10
        """,
        (subject_id, subject_id, subject_id),
    ).fetchall()
    recent_activity = [dict(e) for e in events]

    return {
        "subject_id": subject_id,
        "total_objectives": total_objectives,
        "lessons_written": lessons_written,
        "lessons_stale": lessons_stale,
        "lessons_queued": lessons_queued,
        "queue_by_reason": queue_by_reason,
        "recent_activity": recent_activity,
    }


@app.post("/api/lessons/{objective_id}/regenerate")
def lessons_regenerate_one(objective_id: str, request: Request,
                           response: Response, background_tasks: BackgroundTasks) -> dict:
    """Regenerate one objective's canonical lesson (background). 404 if the objective is
    unknown. Clears is_stale on success."""
    db = request.app.state.db
    row = db.execute(
        "SELECT subject_id FROM objectives WHERE objective_id = ?", (objective_id,)
    ).fetchone()
    if row is None:
        response.status_code = 404
        return {"ok": False, "error": "Unknown objective_id."}
    background_tasks.add_task(_run_regenerate, db, row["subject_id"], [objective_id])
    return {"ok": True, "queued_for": objective_id}


@app.post("/api/lessons/regenerate-stale/{subject_id}")
def lessons_regenerate_stale(subject_id: str, request: Request,
                             background_tasks: BackgroundTasks) -> dict:
    """Regenerate every stale lesson for a subject (background). Returns the count
    queued."""
    db = request.app.state.db
    rows = db.execute(
        "SELECT objective_id FROM objective_lessons "
        "WHERE subject_id = ? AND is_stale = 1",
        (subject_id,),
    ).fetchall()
    objective_ids = [r["objective_id"] for r in rows]
    if objective_ids:
        background_tasks.add_task(_run_regenerate, db, subject_id, objective_ids)
    return {"ok": True, "queued": len(objective_ids)}


@app.get("/api/visual/{objective_id}/status")
def visual_status(objective_id: str, request: Request) -> dict:
    """Check whether a visual has been generated for this objective.

    Returns {exists: bool, cached: bool, generated_at: str|null, generation_ms: int|null}.
    Never triggers generation — use POST /api/visual/{objective_id} for that.
    """
    row = request.app.state.db.execute(
        "SELECT generated_at, generation_ms, file_path FROM visual_pages WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    if not row:
        return {"exists": False, "cached": False, "generated_at": None, "generation_ms": None}
    file_ok = Path(row["file_path"]).exists()
    return {
        "exists": file_ok,
        "cached": file_ok,
        "generated_at": row["generated_at"],
        "generation_ms": row["generation_ms"],
    }


@app.post("/api/visual/{objective_id}")
def visual_generate(objective_id: str, request: Request, response: Response) -> dict:
    """Generate (or serve cached) the visual HTML for one objective.

    First call: calls Gemini Flash (~3-5 s), writes to SSD, records in visual_pages.
    Subsequent calls: instant cache hit (file already on SSD).
    Pass ?force=1 to regenerate even if cached.

    Returns {ok: bool, cached: bool, error: str|null}.
    The visual is then served by GET /api/visual/{objective_id}.
    """
    force = request.query_params.get("force", "0") == "1"
    # Lazy import so the runtime server never loads generate_visual (PHASE: build)
    # until a builder or student actually triggers generation.
    import generate_visual as gv

    result = gv.generate_visual(request.app.state.db, objective_id, force=force)
    if not result["ok"]:
        response.status_code = 400
        return {"ok": False, "cached": False, "error": result["error"]}
    return {"ok": True, "cached": result["cached"], "error": None}


@app.get("/api/visual/{objective_id}")
def visual_serve(objective_id: str, request: Request, response: Response):
    """Serve the generated visual HTML file for an objective.

    Returns the raw HTML file (Content-Type: text/html).
    404 if no visual has been generated yet — call POST first.
    """
    row = request.app.state.db.execute(
        "SELECT file_path FROM visual_pages WHERE objective_id = ?",
        (objective_id,),
    ).fetchone()
    if not row:
        response.status_code = 404
        return {"error": "no visual generated yet — POST /api/visual/{objective_id} first"}

    file_path = Path(row["file_path"])
    if not file_path.exists():
        response.status_code = 404
        return {"error": "visual file missing from SSD — regenerate with POST ?force=1"}

    return FileResponse(str(file_path), media_type="text/html")


@app.post("/api/staging/{subject_id}/auto-accept-and-ingest")
async def staging_auto_accept_and_ingest(subject_id: str, request: Request,
                                         background_tasks: BackgroundTasks) -> dict:
    """Source-authority shortcut (no subject expert in the loop): bulk-accept every
    unreviewed classification at/above a folder-confidence threshold, then ingest the
    lot in one background task.

    Body (optional): {"min_folder_confidence": 70}. Skipped staging rows stay skipped
    and already-decided classifications are left untouched -- this only acts on
    eligible-but-unreviewed classifications. Use this when the uploaded material came
    from authoritative sources (e.g. official CSEC documents) and the builder is
    trusting the source rather than reviewing each classification by subject.
    """
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- empty/non-JSON body is fine
        body = {}
    try:
        min_conf = int(body.get("min_folder_confidence", 70)) if isinstance(body, dict) else 70
    except (TypeError, ValueError):
        min_conf = 70

    rows = db.execute(
        """
        SELECT c.staging_id, c.folder_confidence, c.review_decision, s.skip_classification
        FROM   upload_classifications c
        JOIN   upload_staging s ON s.staging_id = c.staging_id
        WHERE  s.subject_id = ?
        """,
        (subject_id,),
    ).fetchall()

    to_accept, skipped_low, already_decided = [], 0, 0
    for r in rows:
        if r["review_decision"] is not None:
            already_decided += 1
            continue
        if r["skip_classification"]:
            continue  # skipped staging stays skipped -- not a review candidate
        if (r["folder_confidence"] or 0) < min_conf:
            skipped_low += 1
            continue
        to_accept.append(r["staging_id"])

    for sid in to_accept:
        db.execute(
            "UPDATE upload_classifications SET review_decision = 'accepted', "
            "reviewed_at = datetime('now'), "
            "review_notes = 'auto_accepted_source_authority' WHERE staging_id = ?",
            (sid,),
        )
    db.commit()

    # Everything now accepted/overridden and not yet ingested -- the exact set the
    # background ingest will process (a previously-accepted-but-uningested file counts
    # too). Mark them 'queued' for immediate UI feedback, then kick off ingestion.
    eligible = db.execute(
        """
        SELECT s.staging_id FROM upload_staging s
        JOIN upload_classifications c ON c.staging_id = s.staging_id
        WHERE s.subject_id = ?
          AND c.review_decision IN ('accepted', 'overridden')
          AND s.ingestion_status = 'not_started'
        """,
        (subject_id,),
    ).fetchall()
    queued_for_ingestion = len(eligible)
    if queued_for_ingestion:
        db.execute(
            "UPDATE upload_staging SET ingestion_status = 'queued' "
            "WHERE staging_id IN (%s)" % ",".join("?" * queued_for_ingestion),
            [r[0] for r in eligible],
        )
        db.commit()
        background_tasks.add_task(_run_ingest_all, db, subject_id)

    return {
        "ok": True,
        "auto_accepted": len(to_accept),
        "skipped_low_confidence": skipped_low,
        "already_decided": already_decided,
        "queued_for_ingestion": queued_for_ingestion,
    }


@app.get("/api/videos/{objective_id}")
def videos_for_objective(objective_id: str, request: Request) -> dict:
    """Return pre-qualified YouTube videos for one objective.

    Always returns {videos: [...]} — empty list when none exist, never 404.
    Each item: {title, url, channel, duration}.
    """
    rows = request.app.state.db.execute(
        """
        SELECT title, url, channel, duration_str
        FROM   objective_videos
        WHERE  objective_id = ?
        ORDER  BY video_id
        """,
        (objective_id,),
    ).fetchall()
    return {
        "videos": [
            {
                "title":    r["title"],
                "url":      r["url"],
                "channel":  r["channel"],
                "duration": r["duration_str"],
            }
            for r in rows
        ]
    }


@app.post("/api/backup")
def take_backup(body: BackupRequest, response: Response) -> dict:
    """Copy the live DB to a timestamped, labelled backup on the SSD (Stage 14
    backup.py). Build-time safety net, exposed so the UI can take one before a
    destructive bulk action (e.g. auto-accept-and-ingest). 500 if the SSD/DB is
    unavailable -- the caller should not proceed with the destructive action."""
    try:
        from db.backup import backup_database
        path = backup_database(body.label)
        return {"ok": True, "path": path}
    except Exception as exc:  # noqa: BLE001 -- surfaced so the UI can halt
        response.status_code = 500
        return {"ok": False, "error": str(exc)}


# Windows process-creation flags so the spawned .bat survives after uvicorn exits
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _run_shutdown_bat(bat_path: str) -> None:
    """Background task: wait 1 s so the HTTP response flushes, then spawn shutdown.bat."""
    import time
    time.sleep(1)
    subprocess.Popen(
        bat_path,
        shell=True,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


@app.post("/api/shutdown")
def shutdown_system(background_tasks: BackgroundTasks) -> dict:
    """Cleanly stop the study system from the browser.

    Closes the DB, then spawns 00_LAUNCH/shutdown.bat detached so it outlives
    this process.  Returns immediately so the browser receives the response
    before taskkill terminates uvicorn.  Dev-machine fallback: if SSD_ROOT is
    unset or shutdown.bat is absent, returns ok=false with a friendly message.
    """
    # Close the DB first so SQLite WAL is checkpointed before kill.
    try:
        db = app.state.db
        if db is not None:
            db.close()
            app.state.db = None
    except Exception:
        pass

    ssd_root = os.getenv("SSD_ROOT")
    if not ssd_root:
        return {"ok": False, "error": "shutdown script not found - close this window manually"}

    bat_path = str(Path(ssd_root) / "00_LAUNCH" / "shutdown.bat")
    if not os.path.exists(bat_path):
        return {"ok": False, "error": "shutdown script not found - close this window manually"}

    background_tasks.add_task(_run_shutdown_bat, bat_path)
    return {"ok": True, "message": "Stopping the study system..."}
